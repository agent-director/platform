"""Lifecycle failure paths use fake tools only; they never touch a real cluster."""

import inspect
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Mapping
from pathlib import Path
from typing import Any, TextIO, cast

import pytest
from typing_extensions import override

from tests.integration.conftest import (
    REPO_ROOT,
    ClusterContract,
    ForwardManager,
    IntegrationEnvironment,
    discover_workloads,
    lifecycle_values,
    parse_resources,
)

FAKE_LAUNCHER = r"""#!/bin/bash
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
      'create cluster') [[ "${FAIL_STAGE:-}" != creation ]] || exit 42 ;;
      'export kubeconfig'|'load docker-image') ;;
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
      *' get configmap '*lifecycle-contract*) printf '%s' "$(< "$FAKE_CONTRACT_LIST")" ;;
      *' get configmap '*)
        if [[ "${FAIL_STAGE:-}" == ca ]]; then printf '%s' '{"data":{}}';
        else printf '%s' '{"data":{"ca.crt":"public-ca"}}'; fi
        ;;
      *' get secret '*)
        if [[ -n "${FAKE_MISSING_SECRET:-}" && "$args" == *" $FAKE_MISSING_SECRET "* ]]; then
          printf '%s' '{"data":{}}'
        else printf '%s' '{"data":{"username":"cGxhdGZvcm1fdGVzdA==","password":"c3ludGhldGljLXRlc3QtdmFsdWU="},"metadata":{"name":"credential"}}'; fi # gitleaks:allow -- encoded synthetic-test-value, never a real credential
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
      *' wait '*)
        if [[ "${FAIL_STAGE:-}" == serviceaccount && "$args" == *'serviceaccount/'* ]]; then exit 52; fi
        ;;
      *' create namespace '*|*' create secret '*) printf '%s' '{"metadata":{"name":"unit"}}' ;;
      *' apply '*crds*) [[ "${FAIL_STAGE:-}" != crd ]] || exit 46 ;;
      *' apply '*) while IFS= read -r ignored; do :; done ;;
      *) exit 92 ;;
    esac
    ;;
  helm)
    case "$1" in
      list)
        if [[ "${RELEASE_EXISTS:-false}" == true ]]; then
          printf '[{"name":"%s","status":"%s"}]' "$RELEASE_NAME" "${FAKE_RELEASE_STATUS:-deployed}"
        else printf '%s' '[]'; fi
        ;;
      template)
        if [[ "$3" == charts/agent-substrate ]]; then
          if [[ -n "${FAKE_HELM:-}" ]]; then exec "$FAKE_HELM" "$@"; fi
          printf '%s' '{"kind":"Service","spec":{"selector":{"app.kubernetes.io/component":"ateapi"},"ports":[{"name":"grpc","port":50051}]}}'
        else printf '%s' "$(< "$FAKE_CONTRACT_DOCUMENT")"; fi
        ;;
      upgrade)
        if [[ -n "${FAKE_DEPLOY_PID_FILE:-}" && " $* " == *' charts/postgres-operator '* ]]; then
          printf '%s' "$$" > "$FAKE_DEPLOY_PID_FILE"
          exec sleep 30
        fi
        if [[ " $* " == *' bootstrap.enabled=true '* && "${FAIL_STAGE:-}" == bootstrap ]]; then exit 47; fi
        if [[ " $* " == *' bootstrap.enabled=false '* && " $* " == *' workers.enabled=false '* && "${FAIL_STAGE:-}" == server ]]; then exit 48; fi
        if [[ " $* " == *' bootstrap.enabled=false '* && " $* " != *' workers.enabled=false '* && "${FAIL_STAGE:-}" == final ]]; then exit 48; fi
        ;;
      *) exit 93 ;;
    esac
    ;;
  uv)
    case " $* " in
      *' scripts/helm_dependencies.py '*) [[ "${FAIL_STAGE:-}" != dependencies ]] || exit 49 ;;
      *' scripts/toolchain.py image kind '*) printf '%s' 'kindest/node:unit@sha256:fixture' ;;
      *' scripts/toolchain.py build-args '*) printf '%s\n' '--build-arg=PYTHON_VERSION=unit' ;;
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
        (scripts / "mise.sh").write_text(FAKE_LAUNCHER, encoding="utf-8")
        (scripts / "mise.sh").chmod(0o755)
        (directory / "charts" / "postgres-operator" / "crds").mkdir(parents=True)
        image = directory / "images" / "unit"
        image.mkdir(parents=True)
        (image / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        jq = shutil.which("jq")
        assert jq is not None, "The selected toolchain must provide jq for script tests"
        uv = shutil.which("uv")
        assert uv is not None, "Script fixture helpers require uv"
        self.env = {
            **os.environ,
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
            "RELEASE_EXISTS",
        ):
            self.env.pop(key, None)
        self.values = {
            "temporalProfile": "full",
            "workersEnabled": "true",
            "temporalNamespace": "namespace-unit",
            "databaseResource": "different-db-resource",
            "databaseService": "different-db-service",
            "databasePort": "15432",
            "databaseUser": "platform_test",
            "databaseName": "different-platform-db",
            "databaseCredentialSecret": "different-platform-credential",
            "databaseTLSSecret": "different-private-tls",
            "databaseTrustConfigMap": "different-public-trust",
            "databaseTrustKey": "ca.crt",
            "substrateDatabaseUser": "substrate_test",
            "substrateDatabaseName": "different-substrate-db",
            "substrateDatabaseCredentialSecret": "different-substrate-credential",
            "temporalCredentialSecret": "different-temporal-credential",
            "temporalDatabase": "different-temporal-db",
            "temporalVisibilityDatabase": "different-visibility-db",
            "schemaServiceAccount": "different-schema-sa",
            "rustfsSecret": "different-object-secret",
            "rustfsService": "different-object-service",
            "rustfsPort": "19000",
            "temporal-values.yaml": "temporal:\n  server:\n    config:\n      persistence:\n        defaultStore: default\n",
        }
        document = {
            "kind": "ConfigMap",
            "metadata": {
                "labels": {
                    "app.kubernetes.io/component": "lifecycle-contract",
                    "app.kubernetes.io/instance": self.env["RELEASE_NAME"],
                }
            },
            "data": self.values,
        }
        for key, data in (
            ("FAKE_CONTRACT_DOCUMENT", document),
            ("FAKE_CONTRACT_LIST", {"items": [document]}),
        ):
            path = directory / f"{key}.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.env[key] = str(path)

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

    def set_current_contract(self, **changes: str) -> None:
        path = Path(self.env["FAKE_CONTRACT_LIST"])
        document = json.loads(path.read_text())
        document["items"][0]["data"].update(changes)
        path.write_text(json.dumps(document), encoding="utf-8")

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
    """Use real local Helm rendering, with all cluster commands still faked."""
    shutil.copytree(
        REPO_ROOT / "charts" / "agent-substrate",
        harness.root / "charts" / "agent-substrate",
    )
    helm = shutil.which("helm")
    assert helm is not None, "Cross-chart script tests require the selected Helm tool"
    harness.env["FAKE_HELM"] = helm
    return harness


def test_teardown_requires_explicit_name_and_ownership(harness: ScriptHarness) -> None:
    assert harness.run("teardown.sh").returncode != 0
    assert harness.run("teardown.sh", "owned-unit").returncode != 0
    assert not any("delete cluster" in command for command in harness.commands)


def test_existing_cluster_is_not_claimed_or_deleted(harness: ScriptHarness) -> None:
    result = harness.run("deploy.sh", "cluster-up")
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert not harness.marker.exists()
    assert not any(
        "create cluster" in command or "delete cluster" in command
        for command in harness.commands
    )


def test_reused_cluster_does_not_gain_creation_marker(harness: ScriptHarness) -> None:
    harness.env["REUSE_CLUSTER"] = "true"
    assert harness.run("deploy.sh", "cluster-up").returncode == 0
    assert not harness.marker.exists()
    assert harness.run("teardown.sh", "owned-unit").returncode != 0
    assert not any("delete cluster" in command for command in harness.commands)


def test_successful_creation_owns_only_dedicated_kubeconfig(
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
    creation = next(
        command for command in harness.commands if "kind create cluster" in command
    )
    assert f"--kubeconfig {expected}" in creation


def test_failed_creation_writes_no_ownership_and_deletes_nothing(
    harness: ScriptHarness,
) -> None:
    harness.env.update(FAKE_CLUSTERS="", FAIL_STAGE="creation")
    assert harness.run("deploy.sh", "cluster-up").returncode != 0
    assert not harness.marker.exists()
    assert not any("delete cluster" in command for command in harness.commands)


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
    assert not any("delete cluster" in command for command in harness.commands)


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
    assert not any(
        "helm uninstall" in command or "delete namespace" in command
        for command in harness.commands
    )


@pytest.mark.parametrize(
    "stage",
    [
        "dependencies",
        "crd",
        "established",
        "controller",
        "bootstrap",
        "sql",
        "ca",
        "serviceaccount",
    ],
)
def test_failed_prerequisite_never_starts_full_upgrade_or_substrate(
    harness: ScriptHarness, stage: str
) -> None:
    harness.env["FAIL_STAGE"] = stage
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode != 0
    assert not any("bootstrap.enabled=false" in command for command in harness.commands)
    assert not any(
        "helm upgrade --install substrate-release-unit" in command
        for command in harness.commands
    )
    assert not any(
        "delete cluster" in command or "delete namespace" in command
        for command in harness.commands
    )


def test_full_deploy_stages_same_release_after_authenticated_prerequisites(
    harness: ScriptHarness,
) -> None:
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode == 0, result.stderr
    commands = harness.commands
    bootstrap = next(
        index
        for index, command in enumerate(commands)
        if "helm upgrade --install release-unit" in command
    )
    server = next(
        index
        for index, command in enumerate(commands)
        if "helm upgrade release-unit" in command and "workers.enabled=false" in command
    )
    final = next(
        index
        for index, command in enumerate(commands)
        if "helm upgrade release-unit" in command
        and "workers.enabled=false" not in command
    )
    sql = [
        index
        for index, command in enumerate(commands)
        if command.startswith("kubectl ") and " exec " in command
    ]
    assert bootstrap < min(sql) <= max(sql) < server < final
    assert (
        len(sql) == 4
    )  # Platform, substrate, Temporal default and visibility roles/databases.
    assert "bootstrap.prerequisitesReady=true" in commands[final]
    assert "bootstrap.enabled=false" in commands[final]
    assert "workers.enabled=true" in commands[final]
    assert any(
        "different-schema-sa" in command for command in commands[bootstrap:final]
    )
    assert any(
        "different-public-trust" in command for command in commands[bootstrap:final]
    )
    assert any(
        "different-temporal-db" in command for command in commands[bootstrap:final]
    )
    assert not any(
        "different-private-tls" in command and "get secret" in command
        for command in commands
    )
    projection = (
        harness.root / ".cache" / "deploy" / "platform-unit-release-unit-temporal.yaml"
    )
    assert projection.read_text() == harness.values["temporal-values.yaml"] + "\n"
    assert "synthetic-test-value" not in projection.read_text()
    substrate_values = json.loads(
        (
            harness.root
            / ".cache"
            / "deploy"
            / "platform-unit-release-unit-substrate.json"
        ).read_text()
    )
    assert substrate_values["database"]["host"] == "different-db-service"
    assert substrate_values["database"]["port"] == 15432
    assert (
        substrate_values["database"]["credentialSecret"]
        == "different-substrate-credential"
    )
    assert substrate_values["rustfs"]["port"] == 19000


def test_existing_full_upgrade_never_applies_bootstrap_suppression(
    harness: ScriptHarness,
) -> None:
    harness.env["RELEASE_EXISTS"] = "true"
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode == 0, result.stderr
    applied = [
        command for command in harness.commands if command.startswith("helm upgrade")
    ]
    assert not any("bootstrap.enabled=true" in command for command in applied)
    assert any(
        command.startswith("helm template release-unit") for command in harness.commands
    )
    platform = [
        command
        for command in applied
        if command.startswith("helm upgrade release-unit")
    ]
    assert len(platform) == 1
    assert "workers.enabled=false" not in platform[0]


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
    assert not any(
        "delete cluster" in command
        or "docker rm" in command
        or "docker inspect" in command
        for command in harness.commands
    )


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
        assert not any("delete cluster" in command for command in harness.commands)
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
    with pytest.raises(ValueError, match="exactly one"):
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
    assert not any(
        command.startswith("helm upgrade") or " apply " in command
        for command in harness.commands
    )
    (harness.root / ".env").write_text(
        "UNRECOGNIZED=$(touch should-never-exist)\n", encoding="utf-8"
    )
    assert harness.run("deploy.sh", "deploy").returncode == 0
    assert not (harness.root / "should-never-exist").exists()


@pytest.mark.parametrize("stage", ["success", "server", "final"])
def test_application_credentials_never_enter_argv_and_private_values_are_removed(
    harness: ScriptHarness, stage: str
) -> None:
    sensitive = {
        "GH_PAT": "synthetic-package-value",
        "TAILSCALE_AUTH_KEY": "synthetic-tailnet-value",
        "LANGFUSE_NEXTAUTH_SECRET": "synthetic-nextauth-value",
        "LANGFUSE_SALT": "synthetic-salt-value",
    }
    harness.env.update(sensitive, GH_USER="unit-user")
    if stage != "success":
        harness.env["FAIL_STAGE"] = stage
    result = harness.run("deploy.sh", "deploy")
    assert (result.returncode == 0) == (stage == "success"), result.stderr
    commands = "\n".join(harness.commands)
    assert not any(value in commands for value in sensitive.values())
    assert list((harness.root / ".cache" / "deploy").glob("private-values.*")) == []
    upgrades = [
        command
        for command in harness.commands
        if command.startswith("helm upgrade release-unit")
    ]
    if stage == "server":
        assert len(upgrades) == 1 and "workers.enabled=false" in upgrades[0]
    elif stage == "final":
        assert len(upgrades) == 2
        assert (
            "workers.enabled=false" in upgrades[0]
            and "workers.enabled=false" not in upgrades[1]
        )
    if stage != "success":
        assert not any(
            command.startswith("helm upgrade --install substrate-release-unit")
            for command in harness.commands
        )
        assert not any("delete cluster" in command for command in harness.commands)


@pytest.mark.parametrize("changed", ["backend", "server", "absent", "deletion"])
def test_teardown_retains_evidence_when_current_backend_identity_or_deletion_fails(
    harness: ScriptHarness, changed: str
) -> None:
    harness.own_cluster()
    if changed == "backend":
        harness.env["FAKE_BACKEND_UID"] = "another-backend-cluster"
    elif changed == "server":
        harness.env["FAKE_SERVER"] = "https://127.0.0.1:7443"
    elif changed == "absent":
        harness.env["FAKE_CLUSTERS"] = ""
    else:
        harness.env["FAIL_STAGE"] = "deletion"
    result = harness.run("teardown.sh", "owned-unit")
    assert result.returncode != 0
    assert harness.marker.exists()
    assert (harness.root / ".cache/clusters/owned-unit/kubeconfig").exists()
    assert list((harness.root / ".cache/clusters/owned-unit").glob("verify.*")) == []
    deletions = [
        command
        for command in harness.commands
        if command.startswith("kind delete cluster")
    ]
    assert len(deletions) == (1 if changed == "deletion" else 0)
    if changed != "absent":
        assert any(
            command.startswith("kind get kubeconfig --name owned-unit")
            for command in harness.commands
        )


@pytest.mark.parametrize(
    "secret",
    [
        "different-platform-credential",
        "different-substrate-credential",
        "different-temporal-credential",
    ],
)
def test_missing_role_credentials_block_full_upgrade(
    harness: ScriptHarness, secret: str
) -> None:
    harness.env["FAKE_MISSING_SECRET"] = secret
    assert harness.run("deploy.sh", "deploy").returncode != 0
    assert not any("bootstrap.enabled=false" in command for command in harness.commands)


@pytest.mark.parametrize(
    "database",
    ["different-substrate-db", "different-temporal-db", "different-visibility-db"],
)
def test_each_authenticated_database_prerequisite_blocks_full_upgrade(
    harness: ScriptHarness, database: str
) -> None:
    harness.env["FAKE_FAIL_DATABASE"] = database
    assert harness.run("deploy.sh", "deploy").returncode != 0
    assert not any("bootstrap.enabled=false" in command for command in harness.commands)


@pytest.mark.parametrize("profile", ["kind", "full"])
def test_both_temporal_profiles_consume_local_substrate_artifacts(
    harness: ScriptHarness, profile: str
) -> None:
    harness.env["PROFILE"] = profile
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode == 0, result.stderr
    substrate = next(
        command
        for command in harness.commands
        if command.startswith("helm upgrade --install substrate-release-unit")
    )
    assert "ateapi.image.pullPolicy=IfNotPresent" in substrate
    assert "atecontroller.image.pullPolicy=IfNotPresent" in substrate
    final = [
        command
        for command in harness.commands
        if command.startswith("helm upgrade release-unit")
    ][-1]
    assert "-f charts/platform/values-kind.yaml" in final
    assert "workers.substrateNamespace=substrate-unit" in final
    assert "workers.substratePort=50051" in final
    assert "rustfs.substrateNamespace=substrate-unit" in final


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
    platform_override = harness.root / "platform-values.yaml"
    platform_override.write_text("workers:\n  substratePort: 51161\n", encoding="utf-8")
    harness.env["DEPLOY_VALUES"] = str(platform_override)
    result = harness.run(
        "deploy.sh", "deploy", "--", "--set", "workers.health.port=18189"
    )
    assert result.returncode == 0, result.stderr
    commands = harness.commands
    preflight = next(
        index
        for index, command in enumerate(commands)
        if command.startswith("helm template substrate-release-unit")
    )
    first_mutation = next(
        index
        for index, command in enumerate(commands)
        if command.startswith("helm upgrade") or " apply " in command
    )
    assert preflight < first_mutation
    final = [
        command
        for command in commands
        if command.startswith("helm upgrade release-unit")
    ][-1]
    platform_args = shlex.split(final)[4:]
    platform_args = platform_args[: platform_args.index("--wait")]
    # Cleanup intentionally removed this empty private application-settings file.
    private_index = next(
        index
        for index, argument in enumerate(platform_args)
        if "private-values." in argument
    )
    del platform_args[private_index - 1 : private_index + 1]
    platform = subprocess.run(
        [
            harness.env["FAKE_HELM"],
            "template",
            "release-unit",
            str(REPO_ROOT / "charts" / "platform"),
            *platform_args,
            "--show-only",
            "templates/workers.yaml",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    policy = next(
        document
        for document in parse_resources(platform.stdout)
        if document["kind"] == "NetworkPolicy"
    )
    substrate_egress = next(
        rule
        for rule in policy["spec"]["egress"]
        if any(
            peer.get("namespaceSelector", {})
            .get("matchLabels", {})
            .get("kubernetes.io/metadata.name")
            == "substrate-unit"
            and peer.get("podSelector", {})
            .get("matchLabels", {})
            .get("app.kubernetes.io/component")
            == "ateapi"
            for peer in rule.get("to", [])
        )
    )
    assert substrate_egress["ports"] == [{"protocol": "TCP", "port": expected_port}]
    substrate = subprocess.run(
        [
            harness.env["FAKE_HELM"],
            "template",
            "substrate-release-unit",
            str(REPO_ROOT / "charts" / "agent-substrate"),
            *shlex.split(commands[preflight])[4:],
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    documents = parse_resources(substrate.stdout)
    service = next(document for document in documents if document["kind"] == "Service")
    assert service["spec"]["ports"] == [
        {"name": "grpc", "port": expected_port, "targetPort": "grpc"}
    ]
    deployment = next(
        document for document in documents if document["kind"] == "Deployment"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert f"--grpc-listen-addr=0.0.0.0:{expected_port}" in container["args"]
    assert container["ports"] == [{"containerPort": expected_port, "name": "grpc"}]
    ingress = next(
        document for document in documents if document["kind"] == "NetworkPolicy"
    )
    assert ingress["spec"]["ingress"][0]["ports"] == substrate_egress["ports"]
    substrate_upgrade = next(
        command
        for command in commands
        if command.startswith("helm upgrade --install substrate-release-unit")
    )
    assert f"ateapi.service.port={expected_port}" in substrate_upgrade
    if port is not None:
        assert f"-f {harness.env['SUBSTRATE_VALUES']}" in substrate_upgrade


@pytest.mark.parametrize(
    "option", ["--set", "--set-string", "--set-json", "--set-file"]
)
@pytest.mark.parametrize("equals_form", [False, True])
def test_workers_substrate_port_cli_override_is_rejected_before_tools(
    harness: ScriptHarness, option: str, equals_form: bool
) -> None:
    expression = "workers.substratePort=51163"
    arguments = [f"{option}={expression}"] if equals_form else [option, expression]
    result = harness.run("deploy.sh", "deploy", "--", *arguments)
    assert result.returncode != 0
    assert "configure ateapi.service.port through SUBSTRATE_VALUES" in result.stderr
    assert harness.commands == []


@pytest.mark.parametrize("value", ["0", "70000", "not-a-port", "null", "["])
def test_invalid_substrate_port_values_fail_before_cluster_mutation(
    substrate_chart_harness: ScriptHarness, value: str
) -> None:
    harness = substrate_chart_harness
    override = harness.root / "invalid-substrate-values.yaml"
    override.write_text(f"ateapi:\n  service:\n    port: {value}\n", encoding="utf-8")
    harness.env["SUBSTRATE_VALUES"] = str(override)
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode != 0
    assert not any(
        command.startswith("helm upgrade")
        or " apply " in command
        or " create namespace " in command
        for command in harness.commands
    )


def test_shared_multistage_inventory_builds_frontend_and_substrate_outputs(
    harness: ScriptHarness,
) -> None:
    frontend = harness.root / "images/frontend"
    frontend.mkdir()
    (frontend / "Dockerfile").write_text(
        "FROM scratch AS pnpm\nFROM scratch AS builder\nCOPY --from=pnpm /pnpm /pnpm\nFROM scratch AS runtime\nCOPY --from=builder /app /app\n",
        encoding="utf-8",
    )
    substrate = harness.root / "images/agent-substrate"
    substrate.mkdir()
    (substrate / "Dockerfile").write_text(
        "FROM scratch AS builder\nFROM scratch AS ateapi\nCOPY --from=builder /api /api\nFROM scratch AS atecontroller\nCOPY --from=builder /controller /controller\n",
        encoding="utf-8",
    )
    harness.env.update(BUILD_IMAGES="true", IMAGE_TAG="selected-unit-tag")
    result = harness.run("test-integration.sh", "owned-unit")
    assert result.returncode == 0, result.stderr
    builds = [
        command
        for command in harness.commands
        if command.startswith("docker buildx build")
    ]
    assert any(
        "-t ghcr.io/agent-director/frontend:selected-unit-tag" in command
        and "--target runtime" in command
        for command in builds
    )
    assert any(
        "-t ghcr.io/agent-director/ateapi:selected-unit-tag" in command
        and "--target ateapi" in command
        for command in builds
    )
    assert any(
        "-t ghcr.io/agent-director/atecontroller:selected-unit-tag" in command
        and "--target atecontroller" in command
        for command in builds
    )
    assert not any(
        "-t ghcr.io/agent-director/pnpm:" in command
        or "-t ghcr.io/agent-director/runtime:" in command
        for command in builds
    )


def test_reuse_checks_every_tag_loaded_and_inventory_failure_is_not_swallowed(
    harness: ScriptHarness,
) -> None:
    result = harness.run("test-integration.sh", "owned-unit")
    assert result.returncode == 0, result.stderr
    inspections = [
        command
        for command in harness.commands
        if command.startswith("docker image inspect")
    ]
    assert inspections and all(
        "ghcr.io/agent-director/unit:" in command and " unit:" in command
        for command in inspections
    )
    harness.env["FAIL_STAGE"] = "discovery"
    before = len(harness.commands)
    assert harness.run("test-integration.sh", "owned-unit").returncode != 0
    assert not any(
        command.startswith(("docker ", "helm upgrade"))
        for command in harness.commands[before:]
    )


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_cancellation_during_deployment_stops_owned_descendants_only(
    harness: ScriptHarness, signum: int
) -> None:
    pid_file = harness.root / "deployment-child.pid"
    harness.env["FAKE_DEPLOY_PID_FILE"] = str(pid_file)
    unrelated = subprocess.Popen(["sleep", "30"])
    process = subprocess.Popen(
        ["/bin/bash", str(harness.root / "scripts/test-integration.sh"), "owned-unit"],
        cwd=harness.root,
        env=harness.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Launcher exited before fake deployment blocked")
            time.sleep(0.05)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text())
        process.send_signal(signum)
        assert process.wait(timeout=15) == 128 + signum
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert unrelated.poll() is None
        assert not any("delete cluster" in command for command in harness.commands)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)


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


def test_forward_cleanup_escalates_only_the_termination_resistant_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StubbornProcess(FakeForwardProcess):
        @override
        def terminate(self) -> None:
            self.terminated += 1

        @override
        def wait(self, timeout: float | None = None) -> int:
            if self.exit_code is None:
                raise subprocess.TimeoutExpired("owned-kubectl", timeout or 0)
            return self.exit_code

    owned = StubbornProcess()
    unrelated = FakeForwardProcess()

    def launch(_command: list[str], **kwargs: Any) -> Any:
        log = cast(TextIO, kwargs["stdout"])
        log.write("Forwarding from 127.0.0.1:45678 -> 19000\n")
        log.flush()
        return owned

    monkeypatch.setattr(subprocess, "Popen", launch)
    manager = ForwardManager(unit_contract(), tmp_path)
    manager.start("custom-ns", "custom-service", "19000")
    manager.close()
    assert owned.terminated == 1 and owned.killed == 1
    assert unrelated.terminated == 0 and unrelated.killed == 0
    assert manager.forwards[0].log.closed


@pytest.mark.parametrize(
    "failure",
    [
        "profile",
        "workers",
        "credentials",
        "database",
        "api-forward",
        "api-health",
        "after-yield",
    ],
)
def test_fixture_partial_failures_restore_environment_handlers_and_all_owned_forwards(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from tests.integration import conftest as fixtures

    contract = unit_contract()
    values = {
        "temporalProfile": "full" if failure == "profile" else "kind",
        "workersEnabled": "false" if failure == "workers" else "true",
        "databaseService": "custom-db",
        "databasePort": "15432",
        "databaseCredentialSecret": "custom-credential",
        "databaseName": "custom-database",
        "apiService": "custom-api",
        "apiPort": "18080",
        "temporalService": "custom-temporal",
        "temporalPort": "17233",
        "temporalNamespace": "custom-temporal-namespace",
        "temporalTaskQueue": "custom-task-queue",
    }

    def deployment(component: str) -> dict[str, Any]:
        return {
            "kind": "Deployment",
            "metadata": {"name": component},
            "spec": {
                "selector": {"matchLabels": {"app.kubernetes.io/component": component}},
                "template": {
                    "spec": {
                        "volumes": [
                            {"name": "tls", "secret": {"secretName": "custom-tls"}}
                        ]
                    }
                },
            },
        }

    def manifests(
        _self: ClusterContract, release: str, _namespace: str
    ) -> list[dict[str, Any]]:
        if release == contract.release:
            return [
                {
                    "kind": "ConfigMap",
                    "metadata": {
                        "labels": {
                            "app.kubernetes.io/component": "lifecycle-contract",
                            "app.kubernetes.io/instance": release,
                        }
                    },
                    "data": values,
                },
                deployment("platform"),
            ]
        if release == contract.substrate_release:
            return [
                deployment("ateapi"),
                {
                    "kind": "Service",
                    "metadata": {"name": "custom-substrate-api"},
                    "spec": {
                        "selector": {"app.kubernetes.io/component": "ateapi"},
                        "ports": [{"name": "grpc", "port": 15051}],
                    },
                },
            ]
        return [deployment("operator")]

    def from_environment(_environ: Mapping[str, str]) -> ClusterContract:
        return contract

    monkeypatch.setattr(ClusterContract, "from_environment", from_environment)
    monkeypatch.setattr(ClusterContract, "manifest", manifests)
    owned: list[FakeForwardProcess] = []
    logs: list[TextIO] = []
    unrelated = FakeForwardProcess()

    def launch(_command: list[str], **kwargs: Any) -> Any:
        process = FakeForwardProcess(
            1 if failure == "api-forward" and len(owned) == 1 else None
        )
        owned.append(process)
        log = cast(TextIO, kwargs["stdout"])
        logs.append(log)
        if process.poll() is None:
            log.write(f"Forwarding from 127.0.0.1:{45000 + len(owned)} -> 19000\n")
            log.flush()
        return process

    def credentials(
        _contract: ClusterContract, _namespace: str, _secret: str
    ) -> dict[str, str]:
        if failure == "credentials":
            raise ValueError("Synthetic malformed Secret decoding")
        return {"username": "unit-user", "password": "unit-value", "ca.crt": "unit-ca"}

    class ReadyConnection:
        closed = False

        async def close(self) -> None:
            self.closed = True

    readiness = ReadyConnection()

    def database_ready(_endpoint: Any) -> Any:
        if failure == "database":
            raise TimeoutError("Synthetic authenticated SQL readiness failure")
        return readiness

    def api_ready(_url: str) -> None:
        if failure == "api-health":
            raise TimeoutError("Synthetic API readiness failure")

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(fixtures, "secret_data", credentials)
    monkeypatch.setattr(fixtures, "wait_database", database_ready)
    monkeypatch.setattr(fixtures, "wait_api", api_ready)
    monkeypatch.setenv("API_URL", "previous-api-value")
    handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    fixture_factory = cast(
        Callable[
            [pytest.TempPathFactory], Generator[IntegrationEnvironment, None, None]
        ],
        inspect.unwrap(fixtures.integration_env),
    )
    generator = fixture_factory(tmp_path_factory)
    try:
        if failure == "after-yield":
            environment = next(generator)
            assert not readiness.closed
            assert os.environ["API_URL"] == environment.api_url
            with pytest.raises(RuntimeError, match="Synthetic test failure"):
                generator.throw(RuntimeError("Synthetic test failure"))
        else:
            with pytest.raises((ValueError, RuntimeError, TimeoutError)):
                next(generator)
    finally:
        generator.close()
    if failure in {"profile", "workers"}:
        assert not owned
    else:
        assert owned and all(process.exit_code is not None for process in owned)
    assert all(log.closed for log in logs)
    assert os.environ["API_URL"] == "previous-api-value"
    assert all(
        signal.getsignal(signum) == handler for signum, handler in handlers.items()
    )
    assert unrelated.terminated == 0 and unrelated.killed == 0
    if failure in {"api-forward", "api-health", "after-yield"}:
        assert readiness.closed


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


@pytest.mark.parametrize(
    "current_profile,status",
    [
        ("bootstrap", "deployed"),
        ("off", "deployed"),
        ("full", "failed"),
        ("full", "deployed"),
    ],
)
def test_retry_after_bootstrap_or_incomplete_server_replays_namespace_stage_without_live_worker_suppression(
    harness: ScriptHarness, current_profile: str, status: str
) -> None:
    harness.env.update(RELEASE_EXISTS="true", FAKE_RELEASE_STATUS=status)
    harness.set_current_contract(
        temporalProfile=current_profile, workersEnabled="false"
    )
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode == 0, result.stderr
    applied = [
        command
        for command in harness.commands
        if command.startswith("helm upgrade release-unit")
    ]
    assert len(applied) == 2
    assert "workers.enabled=false" in applied[0]
    assert "workers.enabled=false" not in applied[1]
    assert not any(
        command.startswith("helm upgrade --install release-unit")
        for command in harness.commands
    )


@pytest.mark.parametrize(
    "case",
    [
        "kind-to-full",
        "full-to-kind",
        "namespace",
        "kind-namespace",
        "incomplete-active-workers",
    ],
)
def test_unsafe_existing_release_transitions_fail_before_any_cluster_mutation(
    harness: ScriptHarness, case: str
) -> None:
    harness.env["RELEASE_EXISTS"] = "true"
    if case == "kind-to-full":
        harness.set_current_contract(temporalProfile="kind")
    elif case == "full-to-kind":
        harness.env["PROFILE"] = "kind"
    elif case == "namespace":
        harness.set_current_contract(temporalNamespace="existing-other-namespace")
    elif case == "kind-namespace":
        harness.env["PROFILE"] = "kind"
        harness.set_current_contract(
            temporalProfile="kind", temporalNamespace="existing-other-namespace"
        )
    else:
        harness.env["FAKE_RELEASE_STATUS"] = "failed"
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode != 0
    assert not any(
        command.startswith("helm upgrade")
        or " apply " in command
        or " create namespace " in command
        for command in harness.commands
    )
    assert list((harness.root / ".cache/deploy").glob("private-values.*")) == []


def test_sql_probe_deadline_terminates_its_owned_stalled_exec(
    harness: ScriptHarness,
) -> None:
    pid_file = harness.root / "sql-child.pid"
    harness.env.update(FAKE_SQL_PID_FILE=str(pid_file), SQL_PROBE_TIMEOUT="1")
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode != 0
    assert "Authenticated SQL readiness failed" in result.stderr
    assert pid_file.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert not any("bootstrap.enabled=false" in command for command in harness.commands)


@pytest.mark.parametrize(
    "option,expression",
    [
        ("--set", "temporal.enabled=false,temporalDev.enabled=true"),
        ("--set-string", "workers.enabled=false"),
        ("--set-json", 'temporal={"enabled":false}'),
        ("--set-file", "bootstrap.enabled=some-file"),
    ],
)
def test_helm_value_overrides_cannot_bypass_lifecycle_profile_guards(
    harness: ScriptHarness, option: str, expression: str
) -> None:
    harness.env["RELEASE_EXISTS"] = "true"
    result = harness.run("deploy.sh", "deploy", "--", option, expression)
    assert result.returncode != 0
    assert "PROFILE owns" in result.stderr
    assert harness.commands == []


def test_conflicting_values_file_cannot_suppress_a_healthy_full_release(
    harness: ScriptHarness,
) -> None:
    override = harness.root / "conflicting-values.json"
    override.write_text(
        json.dumps(
            {
                "temporal": {"enabled": False},
                "temporalDev": {"enabled": True},
                "workers": {"enabled": False},
                "bootstrap": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )
    harness.env.update(RELEASE_EXISTS="true", DEPLOY_VALUES=str(override))
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode == 0, result.stderr
    upgrades = [
        command
        for command in harness.commands
        if command.startswith("helm upgrade release-unit")
    ]
    assert len(upgrades) == 1
    assert f"-f {override}" in upgrades[0]
    assert (
        "--set temporal.enabled=true --set temporalDev.enabled=false --set workers.enabled=true"
        in upgrades[0]
    )
    assert "--set bootstrap.enabled=false" in upgrades[0]
    assert "workers.enabled=false" not in upgrades[0]


@pytest.mark.parametrize("leader_status", [0, 57])
def test_exited_job_leader_does_not_lose_owned_descendant_cleanup(
    harness: ScriptHarness, leader_status: int
) -> None:
    pid_file = harness.root / "orphan-child.pid"
    harness.env.update(
        FAKE_ORPHAN_PID_FILE=str(pid_file), FAKE_ORPHAN_STATUS=str(leader_status)
    )
    unrelated = subprocess.Popen(["sleep", "30"])
    try:
        result = harness.run("test-integration.sh", "owned-unit")
        assert result.returncode == leader_status, result.stderr
        assert pid_file.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        assert unrelated.poll() is None
        assert not any("delete cluster" in command for command in harness.commands)
        assert list((harness.root / ".cache/integration").glob("clusters.*")) == []
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_signal_during_kind_discovery_cancels_owned_daemon_query(
    harness: ScriptHarness,
) -> None:
    pid_file = harness.root / "discovery-child.pid"
    harness.env["FAKE_DISCOVERY_PID_FILE"] = str(pid_file)
    unrelated = subprocess.Popen(["sleep", "30"])
    process = subprocess.Popen(
        ["/bin/bash", str(harness.root / "scripts/test-integration.sh"), "owned-unit"],
        cwd=harness.root,
        env=harness.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Launcher exited before kind discovery blocked")
            time.sleep(0.05)
        assert pid_file.exists()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 143
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        assert unrelated.poll() is None
        assert list((harness.root / ".cache/integration").glob("clusters.*")) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_deployment_rejects_cached_context_for_a_different_kind_backend(
    harness: ScriptHarness,
) -> None:
    harness.env["FAKE_BACKEND_UID"] = "different-current-kind-cluster"
    result = harness.run("deploy.sh", "deploy")
    assert result.returncode != 0
    assert "different clusters" in result.stderr
    assert not any(
        command.startswith("helm upgrade") or " apply " in command
        for command in harness.commands
    )
    assert list((harness.root / ".cache/clusters/owned-unit").glob("verify.*")) == []
