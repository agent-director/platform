"""Lifecycle failure paths use fake tools only; they never touch a real cluster."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO, cast

import pytest
import yaml

from tests.integration.conftest import (
    REPO_ROOT,
    ClusterContract,
    ForwardManager,
    discover_workloads,
    lifecycle_values,
    parse_resources,
)

FAKE_MISE = r"""#!/bin/bash
set -euo pipefail
[[ "$1" == exec && "$2" == -- ]] || exit 90
shift 2
printf '%s\n' "$*" >> "$FAKE_LOG"
command=$1
shift
if [[ "$command" == jq ]]; then exec "$FAKE_JQ" "$@"; fi
case "$command" in
  kind)
    case "$1 $2" in
      'get clusters')
        if [[ -n "${FAKE_DISCOVERY_PID_FILE:-}" ]]; then
          printf '%s' "$$" > "$FAKE_DISCOVERY_PID_FILE"
          exec sleep 30
        fi
        printf '%s\n' "${FAKE_CLUSTERS:-}"
        ;;
      'create cluster')
        [[ "${FAIL_STAGE:-}" != creation ]] || exit 42
        if [[ -n "${KUBECONFIG:-}" ]]; then
          mkdir -p "$(dirname "$KUBECONFIG")"
          printf '%s\n' 'apiVersion: v1' > "$KUBECONFIG"
        fi
        ;;
      'export kubeconfig')
        if [[ -n "${KUBECONFIG:-}" ]]; then
          mkdir -p "$(dirname "$KUBECONFIG")"
          printf '%s\n' 'apiVersion: v1' > "$KUBECONFIG"
        fi
        ;;
      'load docker-image') ;;
      'get kubeconfig') printf '%s' 'apiVersion: v1' ;;
      'delete cluster') [[ "${FAIL_STAGE:-}" != deletion ]] || exit 51 ;;
      *) exit 91 ;;
    esac
    ;;
  kubectl)
    args=" $* "
    case "$args" in
      *' config view '*) printf '%s' "${FAKE_SERVER:-https://127.0.0.1:6443}" ;;
      *' get namespace kube-system '*)
        if [[ "$args" == *'/verify.'* ]]; then printf '%s' "${FAKE_BACKEND_UID:-${FAKE_UID:-owned-uid}}";
        else printf '%s' "${FAKE_UID:-owned-uid}"; fi
        ;;
      *' get configmap '*)
        if [[ "${FAIL_STAGE:-}" == ca ]]; then printf '%s' '{"data":{}}';
        else printf '%s' '{"data":{"ca.crt":"public-ca"}}'; fi
        ;;
      *' get secret '*)
        if [[ -n "${FAKE_MISSING_SECRET:-}" && "$args" == *" $FAKE_MISSING_SECRET "* ]]; then
          printf '%s' '{"data":{}}'
        else printf '%s' '{"data":{"username":"cGxhdGZvcm1fdGVzdA==","password":"c3ludGhldGljLXRlc3QtdmFsdWU="},"metadata":{"name":"credential"}}'; fi # gitleaks:allow
        ;;
      *' get deployments '*) printf '%s' '{"items":[{"metadata":{"name":"controller-unit"}}]}' ;;
      *' get pods '*) printf '%s' '{"items":[{"metadata":{"name":"db-unit"},"status":{"phase":"Running"}}]}' ;;
      *' exec '*)
        IFS= read -r ignored || true
        [[ "${FAIL_STAGE:-}" != sql ]] || exit 43
        if [[ -n "${FAKE_FAIL_DATABASE:-}" && "$args" == *" $FAKE_FAIL_DATABASE "* ]]; then exit 43; fi
        if [[ -n "${FAKE_SQL_PID_FILE:-}" ]]; then
          printf '%s' "$$" > "$FAKE_SQL_PID_FILE"
          exec sleep 30
        fi
        printf '%s' 1
        ;;
      *' rollout status '*) [[ "${FAIL_STAGE:-}" != controller ]] || exit 44 ;;
      *' wait '*Established*) [[ "${FAIL_STAGE:-}" != established ]] || exit 45 ;;
      *' wait '*) ;;
      *' create namespace '*|*' create secret '*) printf '%s' '{"metadata":{"name":"unit"}}' ;;
      *' apply '*) while IFS= read -r ignored; do :; done ;;
      *) exit 92 ;;
    esac
    ;;
  helm)
    case "$1" in
      repo|dependency) [[ "${FAIL_STAGE:-}" != dependencies ]] || exit 49 ;;
      template)
        if [[ "$3" == charts/agent-substrate ]]; then
          if [[ -n "${FAKE_HELM:-}" ]]; then exec "$FAKE_HELM" "$@"; fi
          printf '%s' '{"kind":"Service","spec":{"selector":{"app.kubernetes.io/component":"ateapi"},"ports":[{"name":"grpc","port":50051}]}}'
        else printf '%s' "$(< "$FAKE_PLATFORM_MANIFEST")"; fi
        ;;
      upgrade)
        if [[ -n "${FAKE_DEPLOY_PID_FILE:-}" && " $* " == *' postgres-operator '* ]]; then
          printf '%s' "$$" > "$FAKE_DEPLOY_PID_FILE"
          exec sleep 30
        fi
        if [[ " $* " == *' charts/platform '* && "${FAIL_STAGE:-}" == platform ]]; then exit 48; fi
        ;;
      *) exit 93 ;;
    esac
    ;;
  uv)
    case " $* " in
      *' scripts/images.py '*)
        [[ "${FAIL_STAGE:-}" != discovery ]] || exit 53
        exec "$FAKE_UV" run --no-project --no-sync --python "$FAKE_PYTHON" python "$FAKE_ROOT/scripts/images.py" --format tsv
        ;;
      *' python -c '*)
        [[ "$1" == run && ( "$2" == --locked || "$2" == --no-project ) && "$3" == python && "$4" == -c ]] || exit 94
        code=$5
        shift 5
        exec "$FAKE_UV" run --no-project --no-sync --python "$FAKE_PYTHON" python -c "$code" "$@"
        ;;
      *' pytest '*)
        if [[ -n "${FAKE_ORPHAN_PID_FILE:-}" ]]; then
          sleep 30 &
          printf '%s' "$!" > "$FAKE_ORPHAN_PID_FILE"
          exit "${FAKE_ORPHAN_STATUS:-0}"
        fi
        if [[ -n "${FAKE_TEST_PID_FILE:-}" ]]; then
          printf '%s' "$$" > "$FAKE_TEST_PID_FILE"
          exec sleep 30
        fi
        [[ "${FAIL_STAGE:-}" != pytest ]] || exit 50
        ;;
      *) exit 94 ;;
    esac
    ;;
  docker) ;;
  *) exit 95 ;;
esac
"""


class ScriptHarness:
    def __init__(self, directory: Path) -> None:
        self.root = directory
        scripts = directory / "scripts"
        scripts.mkdir()
        for name in ("deploy.sh", "teardown.sh", "test-integration.sh", "images.py"):
            shutil.copy(REPO_ROOT / "scripts" / name, scripts / name)
        bin_dir = directory / "bin"
        bin_dir.mkdir()
        (bin_dir / "mise").write_text(FAKE_MISE, encoding="utf-8")
        (bin_dir / "mise").chmod(0o755)
        chart_dir = directory / "charts/platform/charts"
        chart_dir.mkdir(parents=True)
        (chart_dir / "temporal-1.7.0.tgz").write_bytes(b"stub")
        shutil.copy(
            REPO_ROOT / "charts/platform/postgres-operator-values.yaml",
            directory / "charts/platform/postgres-operator-values.yaml",
        )
        image = directory / "images" / "unit"
        image.mkdir(parents=True)
        (image / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        jq = shutil.which("jq")
        assert jq is not None
        uv = shutil.which("uv")
        assert uv is not None
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "FAKE_ROOT": str(directory),
            "FAKE_UV": uv,
            "FAKE_PYTHON": sys.executable,
            "FAKE_LOG": str(directory / "commands.log"),
            "FAKE_JQ": jq,
            "CLUSTER_NAME": "owned-unit",
            "KUBE_CONTEXT": "kind-owned-unit",
            "NAMESPACE": "platform-unit",
            "RELEASE_NAME": "release-unit",
            "SUBSTRATE_NAMESPACE": "substrate-unit",
            "SUBSTRATE_RELEASE_NAME": "substrate-release-unit",
            "PROVIDER_NAMESPACE": "provider-unit",
            "PROVIDER_RELEASE_NAME": "provider-release-unit",
            "PROFILE": "full",
            "READINESS_ATTEMPTS": "1",
            "READINESS_INTERVAL": "0",
            "BUILD_IMAGES": "false",
            "FAKE_CLUSTERS": "owned-unit",
            "KUBECONFIG": str(directory / "global-kubeconfig"),
        }
        for key in (
            "GH_PAT",
            "TAILSCALE_AUTH_KEY",
            "LANGFUSE_SALT",
            "LANGFUSE_NEXTAUTH_SECRET",
            "KUBE_API_URL",
            "DEPLOY_VALUES",
            "SUBSTRATE_VALUES",
            "REUSE_CLUSTER",
            "FAIL_STAGE",
            "FAKE_HELM",
            "FAKE_TEST_PID_FILE",
        ):
            self.env.pop(key, None)
        manifest_docs: list[dict[str, Any]] = [
            {
                "kind": "postgresql",
                "metadata": {"name": "release-unit-db"},
                "spec": {
                    "users": {
                        "platform": (),
                        "langfuse": (),
                        "temporal": (),
                        "agent_substrate": (),
                    },
                    "databases": {
                        "platform": "platform",
                        "langfuse": "langfuse",
                        "temporal": "temporal",
                        "temporal_visibility": "temporal",
                        "agent_substrate": "agent_substrate",
                    },
                },
            },
            {
                "kind": "ConfigMap",
                "metadata": {"name": "release-unit-db-trust"},
                "data": {"ca.crt": "cert"},
            },
            {
                "kind": "Secret",
                "metadata": {"name": "release-unit-platform-rustfs-auth"},
            },
            {
                "kind": "Service",
                "metadata": {"name": "release-unit-platform-rustfs"},
                "spec": {
                    "selector": {"app": "rustfs"},
                    "ports": [{"port": 19000}],
                },
            },
            {
                "kind": "Service",
                "metadata": {"name": "release-unit-platform-unified-api"},
                "spec": {
                    "selector": {"app": "unified-api"},
                    "ports": [{"port": 18000}],
                },
            },
            {
                "kind": "Deployment",
                "metadata": {"name": "release-unit-platform-workers"},
                "spec": {
                    "selector": {"matchLabels": {"app": "workers"}},
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "env": [
                                        {
                                            "name": "TEMPORAL_URL",
                                            "value": "release-unit-temporal-frontend.platform-unit.svc.cluster.local:7233",
                                        },
                                        {
                                            "name": "TEMPORAL_NAMESPACE",
                                            "value": "namespace-unit",
                                        },
                                        {
                                            "name": "TEMPORAL_TASK_QUEUE",
                                            "value": "queue-unit",
                                        },
                                    ]
                                }
                            ]
                        }
                    },
                },
            },
        ]
        manifest_path = directory / "FAKE_PLATFORM_MANIFEST.yaml"
        manifest_path.write_text(yaml.safe_dump_all(manifest_docs), encoding="utf-8")
        self.env["FAKE_PLATFORM_MANIFEST"] = str(manifest_path)
        kubeconfig = self.root / ".cache" / "clusters" / "owned-unit" / "kubeconfig"
        kubeconfig.parent.mkdir(parents=True, exist_ok=True)
        kubeconfig.write_text("owned-local-config", encoding="utf-8")

    @property
    def marker(self) -> Path:
        return self.root / ".cache" / "kind" / "owned-unit.json"

    @property
    def commands(self) -> list[str]:
        log = self.root / "commands.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def run(self, name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(self.root / "scripts" / name), *arguments],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def own_cluster(self) -> None:
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        kubeconfig = self.root / ".cache" / "clusters" / "owned-unit" / "kubeconfig"
        kubeconfig.parent.mkdir(parents=True, exist_ok=True)
        kubeconfig.write_text("owned-local-config", encoding="utf-8")
        self.marker.write_text(
            json.dumps(
                {
                    "name": "owned-unit",
                    "context": "kind-owned-unit",
                    "uid": "owned-uid",
                    "server": "https://127.0.0.1:6443",
                    "root": str(self.root),
                    "kubeconfig": str(kubeconfig),
                    "created": True,
                }
            ),
            encoding="utf-8",
        )


@pytest.fixture
def harness(tmp_path: Path) -> ScriptHarness:
    return ScriptHarness(tmp_path)


@pytest.fixture
def substrate_chart_harness(harness: ScriptHarness) -> ScriptHarness:
    shutil.copytree(
        REPO_ROOT / "charts" / "agent-substrate",
        harness.root / "charts" / "agent-substrate",
    )
    helm = shutil.which("helm")
    assert helm is not None
    harness.env["FAKE_HELM"] = helm
    return harness


def test_teardown_requires_ownership(harness: ScriptHarness) -> None:
    assert harness.run("teardown.sh").returncode != 0
    assert harness.run("teardown.sh", "owned-unit").returncode != 0
    assert not any("delete cluster" in command for command in harness.commands)


def test_existing_cluster_is_not_claimed_or_deleted(harness: ScriptHarness) -> None:
    result = harness.run("deploy.sh", "cluster-up")
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert not harness.marker.exists()


def test_reused_cluster_does_not_gain_creation_marker(harness: ScriptHarness) -> None:
    harness.env["REUSE_CLUSTER"] = "true"
    assert harness.run("deploy.sh", "cluster-up").returncode == 0
    assert not harness.marker.exists()
    assert harness.run("teardown.sh", "owned-unit").returncode != 0


def test_successful_creation_owns_only_dedicated_kubeconfig_and_binds_loopback(
    harness: ScriptHarness,
) -> None:
    harness.env["FAKE_CLUSTERS"] = ""
    global_config = Path(harness.env["KUBECONFIG"])
    global_config.write_text("global-context-unchanged", encoding="utf-8")
    result = harness.run("deploy.sh", "cluster-up")
    assert result.returncode == 0, result.stderr
    marker = json.loads(harness.marker.read_text())
    expected = harness.root / ".cache" / "clusters" / "owned-unit" / "kubeconfig"
    assert marker["kubeconfig"] == str(expected)
    assert marker["created"] is True
    assert global_config.read_text() == "global-context-unchanged"
    config = (harness.root / ".cache/kind/owned-unit-config.yaml").read_text()
    assert 'apiServerAddress: "127.0.0.1"' in config


def test_check_cluster_rejects_non_loopback_api_server(harness: ScriptHarness) -> None:
    harness.env["FAKE_SERVER"] = "https://0.0.0.0:6443"
    result = harness.run("deploy.sh", "check-cluster")
    assert result.returncode != 0
    assert "127.0.0.1" in result.stderr


def test_check_cluster_fails_clearly_when_kubeconfig_missing(
    harness: ScriptHarness,
) -> None:
    (harness.root / ".cache/clusters/owned-unit/kubeconfig").unlink()
    result = harness.run("deploy.sh", "check-cluster")
    assert result.returncode != 0
    assert "make cluster-up" in result.stderr


@pytest.mark.parametrize("change", ["identity", "root", "kubeconfig"])
def test_teardown_rejects_changed_or_foreign_cluster(
    harness: ScriptHarness, change: str
) -> None:
    harness.own_cluster()
    if change == "identity":
        harness.env["FAKE_UID"] = "replacement-cluster"
    else:
        marker = json.loads(harness.marker.read_text())
        marker[change] = "/foreign/path"
        harness.marker.write_text(json.dumps(marker), encoding="utf-8")
    assert harness.run("teardown.sh", "owned-unit").returncode != 0
    assert harness.marker.exists()


def test_teardown_deletes_only_the_named_owned_kind_cluster(
    harness: ScriptHarness,
) -> None:
    harness.own_cluster()
    result = harness.run("teardown.sh", "owned-unit")
    assert result.returncode == 0, result.stderr
    assert not harness.marker.exists()
    deletions = [command for command in harness.commands if "delete cluster" in command]
    assert len(deletions) == 1
    assert "--name owned-unit" in deletions[0]


@pytest.mark.parametrize(
    "stage",
    [
        "established",
        "controller",
        "platform",
        "sql",
        "ca",
    ],
)
def test_failed_prerequisite_never_starts_substrate(
    harness: ScriptHarness, stage: str
) -> None:
    harness.env["FAIL_STAGE"] = stage
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode != 0
    assert not any(
        "helm upgrade --install substrate-release-unit" in command
        for command in harness.commands
    )


def test_full_deploy_installs_direct_operator_platform_and_substrate(
    harness: ScriptHarness,
) -> None:
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode == 0, result.stderr
    commands = harness.commands
    operator = next(
        index
        for index, command in enumerate(commands)
        if "helm upgrade --install provider-release-unit postgres-operator" in command
    )
    platform = next(
        index
        for index, command in enumerate(commands)
        if "helm upgrade --install release-unit charts/platform" in command
    )
    substrate = next(
        index
        for index, command in enumerate(commands)
        if "helm upgrade --install substrate-release-unit charts/agent-substrate"
        in command
    )
    sql = [
        index
        for index, command in enumerate(commands)
        if command.startswith("kubectl ") and " exec " in command
    ]
    assert operator < platform < min(sql) <= max(sql) < substrate
    assert len(sql) == 4
    assert (
        "--repo https://opensource.zalando.com/postgres-operator/charts/postgres-operator"
        in commands[operator]
    )
    assert "--version 2.0.3" in commands[operator]


def test_cluster_context_mismatch_fails_before_any_tool(harness: ScriptHarness) -> None:
    harness.env["KUBE_CONTEXT"] = "kind-unrelated"
    assert harness.run("deploy.sh", "deploy").returncode != 0
    assert harness.commands == []


def test_integration_preserves_cluster_after_pytest_failure(
    harness: ScriptHarness,
) -> None:
    harness.env["FAIL_STAGE"] = "pytest"
    result = harness.run("test-integration.sh", "owned-unit")
    assert result.returncode != 0
    assert any(
        "pytest tests/integration/ -n 0" in command for command in harness.commands
    )
    assert not any("delete cluster" in command for command in harness.commands)


def test_integration_signal_terminates_only_its_owned_child(
    harness: ScriptHarness,
) -> None:
    pid_file = harness.root / "test-child.pid"
    harness.env["FAKE_TEST_PID_FILE"] = str(pid_file)
    unrelated = subprocess.Popen(["sleep", "30"])
    process = subprocess.Popen(
        [
            "/bin/bash",
            str(harness.root / "scripts" / "test-integration.sh"),
            "owned-unit",
        ],
        cwd=harness.root,
        env=harness.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(
                    "Integration launcher exited before signal-test child started"
                )
            time.sleep(0.05)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text())
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 143
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert unrelated.poll() is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)


class FakeForwardProcess:
    def __init__(self, exit_code: int | None = None) -> None:
        self.exit_code = exit_code
        self.terminated = 0
        self.killed = 0

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated += 1
        self.exit_code = -signal.SIGTERM

    def kill(self) -> None:
        self.killed += 1
        self.exit_code = -signal.SIGKILL

    def wait(self, timeout: float | None = None) -> int:
        assert self.exit_code is not None
        return self.exit_code


def unit_contract() -> ClusterContract:
    return ClusterContract(
        "unique-unit",
        "kind-unique-unit",
        str(REPO_ROOT / ".cache/clusters/unique-unit/kubeconfig"),
        "custom-ns",
        "custom-release",
        "custom-substrate",
        "custom-substrate-release",
        "custom-provider",
        "custom-provider-release",
        "kind",
    )


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_forward_manager_owns_random_bindings_and_cleans_all_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    owned = FakeForwardProcess(1 if outcome == "failure" else None)
    unrelated = FakeForwardProcess()
    recorded: list[str] = []

    def launch(command: list[str], **kwargs: Any) -> Any:
        recorded.extend(command)
        log = cast(TextIO, kwargs["stdout"])
        if outcome == "success":
            log.write("Forwarding from 127.0.0.1:45678 -> 19000\n")
            log.flush()
        return owned

    monkeypatch.setattr(subprocess, "Popen", launch)
    manager = ForwardManager(unit_contract(), tmp_path)
    if outcome == "success":
        assert manager.start("custom-ns", "custom-service", "19000") == 45678
    elif outcome == "failure":
        with pytest.raises(RuntimeError, match="exited"):
            manager.start("custom-ns", "custom-service", "19000")
    else:
        with pytest.raises(TimeoutError, match="did not become ready"):
            manager.start("custom-ns", "custom-service", "19000", timeout=0.001)
    manager.close()
    assert ":19000" in recorded
    assert "45678:19000" not in recorded
    assert "kind-unique-unit" in recorded and "custom-ns" in recorded
    assert owned.terminated == (0 if outcome == "failure" else 1)
    assert all(forward.log.closed for forward in manager.forwards)
    assert unrelated.terminated == 0 and unrelated.killed == 0


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_forward_signal_handler_closes_only_its_own_processes(
    tmp_path: Path, signum: int
) -> None:
    manager = ForwardManager(unit_contract(), tmp_path)
    owned = FakeForwardProcess()
    unrelated = FakeForwardProcess()
    from tests.integration.conftest import OwnedForward

    log = (tmp_path / "forward.log").open("w", encoding="utf-8")
    manager.forwards.append(
        OwnedForward(cast(subprocess.Popen[str], owned), log, tmp_path / "forward.log")
    )
    with pytest.raises((KeyboardInterrupt, SystemExit)):
        manager.handle_signal(signum, None)
    assert owned.terminated == 1
    assert unrelated.terminated == 0
    assert log.closed


def test_discovery_fails_loudly_instead_of_reducing_smoke_coverage() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_resources("")
    with pytest.raises(ValueError, match="No workloads"):
        discover_workloads([{"kind": "ConfigMap"}], "custom-ns")
    with pytest.raises(ValueError, match="no name or selector"):
        discover_workloads(
            [{"kind": "Deployment", "metadata": {"name": "broken"}, "spec": {}}],
            "custom-ns",
        )
    with pytest.raises(ValueError, match="Expected"):
        lifecycle_values([], "custom-release")


def test_integration_contract_requires_explicit_endpoints_and_dedicated_config() -> (
    None
):
    with pytest.raises(ValueError, match="missing"):
        ClusterContract.from_environment({})
    values = {
        "CLUSTER_NAME": "unique-unit",
        "KUBE_CONTEXT": "kind-unique-unit",
        "KUBECONFIG": str(REPO_ROOT / ".cache/clusters/unique-unit/kubeconfig"),
        "NAMESPACE": "custom-ns",
        "RELEASE_NAME": "custom-release",
        "SUBSTRATE_NAMESPACE": "custom-substrate",
        "SUBSTRATE_RELEASE_NAME": "custom-substrate-release",
        "PROVIDER_NAMESPACE": "custom-provider",
        "PROVIDER_RELEASE_NAME": "custom-provider-release",
        "PROFILE": "kind",
    }
    assert ClusterContract.from_environment(values) == unit_contract()
    values["KUBECONFIG"] = "/foreign/global-kubeconfig"
    with pytest.raises(ValueError, match="dedicated"):
        ClusterContract.from_environment(values)


@pytest.mark.parametrize(
    "option", ["--namespace", "--kubeconfig", "--kube-apiserver", "--post-renderer"]
)
def test_helm_destination_passthrough_is_rejected_before_tools(
    harness: ScriptHarness, option: str
) -> None:
    result = harness.run("deploy.sh", "deploy", "--", option, "unrelated")
    assert result.returncode != 0
    assert "destination/transport" in result.stderr
    assert harness.commands == []


def test_dotenv_cannot_replace_destination_or_execute_shell(
    harness: ScriptHarness,
) -> None:
    (harness.root / ".env").write_text(
        "KUBE_CONTEXT=production\nKUBECONFIG=/foreign/global\n", encoding="utf-8"
    )
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode != 0
    assert "forbidden in .env" in result.stderr
    (harness.root / ".env").write_text(
        "UNRECOGNIZED=$(touch should-never-exist)\n", encoding="utf-8"
    )
    assert harness.run("deploy.sh", "deploy").returncode == 0
    assert not (harness.root / "should-never-exist").exists()


@pytest.mark.parametrize("port", [None, 51159])
def test_substrate_chart_port_owns_script_and_workers_egress(
    substrate_chart_harness: ScriptHarness, port: int | None
) -> None:
    harness = substrate_chart_harness
    harness.env["PROFILE"] = "kind"
    expected_port = 50051 if port is None else port
    if port is not None:
        override = harness.root / "substrate-values.yaml"
        override.write_text(
            f"ateapi:\n  service:\n    port: {port}\n", encoding="utf-8"
        )
        harness.env["SUBSTRATE_VALUES"] = str(override)
    result = harness.run(
        "deploy.sh", "deploy", "--", "--set", "workers.health.port=18189"
    )
    assert result.returncode == 0, result.stderr
    commands = harness.commands
    final = [
        command
        for command in commands
        if command.startswith("helm upgrade --install release-unit")
    ][-1]
    assert f"workers.substratePort={expected_port}" in final
    substrate_upgrade = next(
        command
        for command in commands
        if command.startswith("helm upgrade --install substrate-release-unit")
    )
    assert f"ateapi.service.port={expected_port}" in substrate_upgrade


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("scoped", [False, True])
def test_signal_during_forward_spawn_is_deferred_until_child_is_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signum: int, scoped: bool
) -> None:
    manager = ForwardManager(unit_contract(), tmp_path)
    owned = FakeForwardProcess()
    unrelated = FakeForwardProcess()
    previous = signal.getsignal(signum)
    signal.signal(signum, manager.handle_signal)

    def launch(_command: list[str], **_kwargs: Any) -> Any:
        handler = signal.getsignal(signum)
        assert callable(handler)
        handler(signum, None)
        return owned

    monkeypatch.setattr(subprocess, "Popen", launch)
    try:
        with pytest.raises((KeyboardInterrupt, SystemExit)):
            if scoped:
                with manager.scoped_forward("custom-ns", "custom-service", "19000"):
                    pytest.fail("Interrupted scoped forward must not yield")
            else:
                manager.start("custom-ns", "custom-service", "19000")
    finally:
        signal.signal(signum, previous)
    assert len(manager.forwards) == 1
    assert owned.terminated == 1
    assert unrelated.terminated == 0
    assert manager.forwards[0].log.closed


def test_scoped_forward_closes_only_its_child_in_shared_signal_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[FakeForwardProcess] = []

    def launch(_command: list[str], **kwargs: Any) -> Any:
        process = FakeForwardProcess()
        processes.append(process)
        log = cast(TextIO, kwargs["stdout"])
        log.write(f"Forwarding from 127.0.0.1:{45000 + len(processes)} -> 19000\n")
        log.flush()
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    manager = ForwardManager(unit_contract(), tmp_path)
    manager.start("custom-ns", "session-service", "19000")
    with manager.scoped_forward("custom-ns", "test-service", "19000") as port:
        assert port == 45002
        assert all(process.poll() is None for process in processes)
    assert processes[0].terminated == 0
    assert processes[1].terminated == 1
    manager.close()
    assert processes[0].terminated == 1
    assert processes[1].terminated == 1
    assert all(forward.log.closed for forward in manager.forwards)


def test_database_readiness_deadline_covers_a_stalled_query_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from tests.integration import conftest as fixtures

    class StalledConnection:
        closed = False

        async def close(self) -> None:
            self.closed = True

        async def execute(self, _query: str) -> Any:
            await asyncio.Event().wait()

    connection = StalledConnection()

    async def connect(_conninfo: str, **kwargs: Any) -> Any:
        assert kwargs["autocommit"] is True
        return connection

    monkeypatch.setattr(fixtures.psycopg.AsyncConnection, "connect", connect)
    endpoint = fixtures.DatabaseEndpoint(
        "127.0.0.1", 15432, "unit-user", "unit-value", "custom-db"
    )
    with pytest.raises(TimeoutError, match="Authenticated PostgreSQL"):
        fixtures.wait_database(endpoint, timeout=0.01)
    assert connection.closed
