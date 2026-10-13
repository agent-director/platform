"""Security gate controls use nonfunctional values assembled only at runtime."""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import render_profiles

ROOT = Path(__file__).absolute().parents[2]


def _synthetic_token() -> str:
    # A complete token exists only as deterministic, nonfunctional runtime data.
    prefix = "ghp_"
    return (
        prefix
        + hashlib.sha256(b"nonfunctional scanner positive control").hexdigest()[:36]
    )


def _hook_binary(hook: str) -> str:
    prek = shutil.which("prek")
    assert prek is not None, "Run security controls through the root mise toolchain"
    result = subprocess.run(
        [prek, "exec", hook, "--", "which", hook],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    executable = result.stdout.strip()
    assert Path(executable).is_file(), result.stdout + result.stderr
    return executable


@pytest.fixture(scope="session")
def gitleaks_binary() -> str:
    return _hook_binary("gitleaks")


@pytest.fixture(scope="session")
def kube_linter_binary() -> str:
    return _hook_binary("kube-linter")


def _scan(
    directory: Path, executable: str
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    result = subprocess.run(
        [
            executable,
            "dir",
            "--config",
            str(ROOT / ".gitleaks.toml"),
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            "report.json",
            ".",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    report = directory / "report.json"
    assert report.is_file(), result.stderr
    return result, json.loads(report.read_text())


@pytest.mark.parametrize(
    "filename",
    [
        "source.txt",
        "charts/component/key.txt",
        ".secrets.baseline.backup",
        "source/.secrets.baseline",
    ],
)
def test_gitleaks_inherited_rules_detect_first_party_and_baseline_lookalikes(
    tmp_path: Path, filename: str, gitleaks_binary: str
) -> None:
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("token = " + _synthetic_token() + "\n")
    result, findings = _scan(tmp_path, gitleaks_binary)
    assert result.returncode == 1, result.stdout + result.stderr
    assert any(
        finding["RuleID"] == "github-pat" and finding["File"].endswith(filename)
        for finding in findings
    )


def test_gitleaks_exempts_only_baseline_and_generated_state(
    tmp_path: Path, gitleaks_binary: str
) -> None:
    exempt = [
        ".secrets.baseline",
        ".cache/integration/test.env",
        ".venv/config.txt",
        "node_modules/config.txt",
        "frontend/node_modules/config.txt",
        "frontend/.cache/config.txt",
        "frontend/.next/config.txt",
    ]
    for filename in exempt:
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("token = " + _synthetic_token() + "\n")
    result, findings = _scan(tmp_path, gitleaks_binary)
    assert result.returncode == 0, result.stdout + result.stderr
    assert findings == []
    # A clean-tree run must not silently scan zero rules/zero files; the same
    # invocation now fails when an identical value is placed in first-party code.
    (tmp_path / "first-party.txt").write_text("token = " + _synthetic_token() + "\n")
    result, findings = _scan(tmp_path, gitleaks_binary)
    assert result.returncode == 1, result.stdout + result.stderr
    assert any(finding["File"].endswith("first-party.txt") for finding in findings)


def test_scanner_policy_preserves_complementary_enforcement() -> None:
    gitleaks = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert gitleaks["extend"]["useDefault"] is True
    assert not gitleaks["extend"].get("disabledRules")
    baseline_regex = gitleaks["allowlists"][0]["paths"][0]
    assert re.fullmatch(baseline_regex, ".secrets.baseline")
    assert not re.search(baseline_regex, ".secrets.baseline.backup")
    assert not re.search(baseline_regex, "source/.secrets.baseline")

    kube_linter = yaml.safe_load((ROOT / ".kube-linter.yaml").read_text())["checks"]
    assert kube_linter["doNotAutoAddDefaults"] is False
    assert {"no-readiness-probe", "no-liveness-probe"}.issubset(kube_linter["include"])
    assert not kube_linter.get("exclude")
    operator = yaml.safe_load((ROOT / ".kube-linter-operator.yaml").read_text())[
        "checks"
    ]
    assert operator["doNotAutoAddDefaults"] is False
    assert set(operator["exclude"]) == {"no-liveness-probe", "readiness-port"}
    assert {"no-readiness-probe", "no-liveness-probe"}.issubset(operator["include"])
    for template in (ROOT / "charts").glob("*/templates/*"):
        if template.is_file():
            assert "ignore-check.kube-linter.io/all" not in template.read_text(), (
                template
            )

    trivy = yaml.safe_load((ROOT / "trivy.yaml").read_text())
    assert set(trivy["scan"]["scanners"]) == {"misconfig", "secret"}
    assert set(trivy["severity"]) == {"HIGH", "CRITICAL"}
    assert trivy["exit-code"] == 1
    assert trivy["cache-dir"] == ".cache/trivy"
    assert (
        "db" not in trivy
    )  # No repository policy leaks stale DB settings into image scans.


@pytest.mark.parametrize(
    "violation", ["no-liveness-probe", "no-readiness-probe", "run-as-non-root"]
)
def test_kube_linter_real_default_and_probe_positive_controls(
    tmp_path: Path, kube_linter_binary: str, violation: str
) -> None:
    container: dict[str, Any] = {
        "name": "app",
        "image": "busybox:1.37.0",
        "command": ["sleep", "3600"],
        "resources": {
            "requests": {"cpu": "10m", "memory": "16Mi"},
            "limits": {"cpu": "100m", "memory": "32Mi"},
        },
        "securityContext": {
            "runAsUser": 1000,
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "livenessProbe": {"exec": {"command": ["/bin/true"]}},
        "readinessProbe": {"exec": {"command": ["/bin/true"]}},
    }
    document = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "security-control", "namespace": "fixture"},
        "spec": {"containers": [container]},
    }
    path = tmp_path / "pod.yaml"
    path.write_text(yaml.safe_dump(document))
    command = [
        kube_linter_binary,
        "lint",
        "--config",
        str(ROOT / ".kube-linter.yaml"),
        "--format",
        "json",
        str(path),
    ]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if violation == "no-liveness-probe":
        container.pop("livenessProbe")
    elif violation == "no-readiness-probe":
        container.pop("readinessProbe")
    else:
        container["securityContext"].update(runAsUser=0, runAsNonRoot=False)
    path.write_text(yaml.safe_dump(document))
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert violation in result.stdout + result.stderr
    assert "security-control" in result.stdout + result.stderr


def test_kube_linter_scans_enabled_kind_workload_not_only_defaults(
    tmp_path: Path, kube_linter_binary: str
) -> None:
    rendered = tmp_path / "profiles"
    rendered.mkdir(mode=0o700)
    render_profiles.render_profiles(ROOT, rendered)
    path = rendered / "kind.yaml"
    command = [
        kube_linter_binary,
        "lint",
        "--config",
        str(ROOT / ".kube-linter.yaml"),
        str(path),
    ]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    documents = [
        document for document in yaml.safe_load_all(path.read_text()) if document
    ]
    temporal = [
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and any(
            container["name"] == "temporal-dev"
            for container in document["spec"]["template"]["spec"]["containers"]
        )
    ]
    assert len(temporal) == 1, (
        "Kind profile must actually contain its Temporal CLI workload"
    )
    temporal[0]["spec"]["template"]["spec"]["containers"][0].pop("livenessProbe")
    path.write_text(yaml.safe_dump_all(documents))
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "no-liveness-probe" in result.stdout + result.stderr
    assert temporal[0]["metadata"]["name"] in result.stdout + result.stderr


def _trivy_scan(
    directory: Path, *, ignore_exceptions: bool = True
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    executable = shutil.which("trivy")
    assert executable is not None, "Run IaC controls through the root mise toolchain"
    # Keep the config's relative ignorefile resolution identical to make scan-iac.
    shutil.copy2(ROOT / ".trivyignore.yaml", directory / ".trivyignore.yaml")
    command = [
        executable,
        "fs",
        "--skip-check-update",
        "--offline-scan",
        "--skip-version-check",
        "--disable-telemetry",
        "--timeout",
        "30s",
        "--config",
        str(ROOT / "trivy.yaml"),
        "--cache-dir",
        str(ROOT / ".cache/trivy"),
        "--include-non-failures",
        "--format",
        "json",
    ]
    if not ignore_exceptions:
        command.extend(["--ignorefile", ""])
    result = subprocess.run(
        [*command, "."], cwd=directory, capture_output=True, text=True, check=False
    )
    assert result.returncode in {0, 1}, result.stdout + result.stderr
    assert result.stdout.strip().startswith("{"), result.stdout + result.stderr
    assert "failed to parse" not in result.stderr.lower(), result.stderr
    return result, json.loads(result.stdout)


def _trivy_failures(report: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (item["Target"], finding["ID"])
        for item in report.get("Results", [])
        for finding in (item.get("Misconfigurations") or ())
        if finding["Status"] == "FAIL"
    }


def _copy_chart(directory: Path, name: str) -> Path:
    destination = directory / "charts" / name
    shutil.copytree(ROOT / "charts" / name, destination)
    return destination


def test_trivy_exception_policy_is_exact_and_version_guarded() -> None:
    config = yaml.safe_load((ROOT / "trivy.yaml").read_text())
    assert config["ignorefile"] == ".trivyignore.yaml"
    assert set(config["scan"]["skip-dirs"]) == {
        ".cache",
        ".venv",
        "node_modules",
        "frontend/node_modules",
        "frontend/.cache",
        "frontend/.next",
        ".git",
    }
    assert set(config["scan"]["skip-files"]) == {
        ".secrets.baseline",
        "charts/platform/templates/lifecycle-contract.yaml",
    }
    policy = yaml.safe_load((ROOT / ".trivyignore.yaml").read_text())
    assert set(policy) == {"misconfigurations"}
    dependency_path = "charts/postgres-operator/charts/postgres-operator/templates/"
    expected = {
        (dependency_path + "clusterrole-postgres-pod.yaml", "KSV-0056"),
        (dependency_path + "clusterrole.yaml", "KSV-0041"),
        (dependency_path + "clusterrole.yaml", "KSV-0053"),
        (dependency_path + "clusterrole.yaml", "KSV-0056"),
        (dependency_path + "deployment.yaml", "KSV-0118"),
    }
    entries = policy["misconfigurations"]
    assert len(entries) == len(expected)
    assert {
        (entry["paths"][0], entry["id"])
        for entry in entries
        if len(entry["paths"]) == 1 and entry.get("statement")
    } == expected
    # Upgrades must explicitly re-review exceptions and native virtual-path behavior.
    assert tomllib.loads((ROOT / "mise.toml").read_text())["tools"]["trivy"] == "0.75.0"
    wrapper = ROOT / "charts/postgres-operator"
    metadata = yaml.safe_load((wrapper / "Chart.yaml").read_text())
    assert metadata["dependencies"][0]["version"] == "2.0.3"
    assert (
        yaml.safe_load((wrapper / "Chart.lock").read_text())["dependencies"]
        == (metadata["dependencies"])
    )
    with tarfile.open(wrapper / "charts/postgres-operator-2.0.3.tgz") as artifact:
        deployment = artifact.extractfile("postgres-operator/templates/deployment.yaml")
        assert deployment is not None
        template = deployment.read().decode()
    assert "toYaml .Values.securityContext" in template
    assert "podSecurityContext" not in template
    assert template.count("securityContext:") == 1
    # The pod-context exception is justified only while the actual container
    # keeps every promised restriction, including checks below Trivy's HIGH floor.
    security = yaml.safe_load((wrapper / "values.yaml").read_text())[
        "postgres-operator"
    ]["securityContext"]
    assert security["runAsUser"] == 10001
    assert security["runAsNonRoot"] is True
    assert security["readOnlyRootFilesystem"] is True
    assert security["allowPrivilegeEscalation"] is False
    assert security["capabilities"]["drop"] == ["ALL"]
    assert security["seccompProfile"]["type"] == "RuntimeDefault"
    assert security.get("privileged", False) is False


def test_trivy_real_iac_misconfiguration_positive_control(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM alpine:3.20.0\nUSER 1000\nHEALTHCHECK CMD /bin/true\n")
    result, _ = _trivy_scan(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    dockerfile.write_text("FROM alpine:3.20.0\nHEALTHCHECK CMD /bin/true\n")
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ("Dockerfile", "DS-0002") in _trivy_failures(report)


def test_trivy_substrate_inline_exception_does_not_hide_adjacent_rule(
    tmp_path: Path,
) -> None:
    chart = _copy_chart(tmp_path, "agent-substrate")
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    target = "charts/agent-substrate/templates/rbac.yaml"
    assert any(
        item["Target"] == target and item["MisconfSummary"]["Successes"] > 0
        for item in report["Results"]
    )
    rbac = chart / "templates/rbac.yaml"
    original = rbac.read_text()
    assert original.count("# trivy:ignore:KSV-0056") == 1
    rbac.write_text(original.replace("# trivy:ignore:KSV-0056\n", ""))
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _trivy_failures(report) == {(target, "KSV-0056")}
    # Restore the accepted network-policy rule, then introduce the same check's
    # Service-management violation on the neighboring first-party rule.
    rule = '  resources: ["pods"]\n  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]'
    assert original.count(rule) == 1
    rbac.write_text(
        original.replace(rule, rule.replace('["pods"]', '["pods", "services"]'))
    )
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _trivy_failures(report) == {(target, "KSV-0056")}
    assert any(
        "services" in finding["Message"]
        for item in report["Results"]
        for finding in (item.get("Misconfigurations") or ())
        if finding["Status"] == "FAIL"
    )


def test_trivy_operator_scanned_and_same_ids_fail_on_first_party_lookalike(
    tmp_path: Path,
) -> None:
    chart = _copy_chart(tmp_path, "postgres-operator")
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    dependency_path = "charts/postgres-operator/charts/postgres-operator/templates/"
    for filename in [
        "clusterrole.yaml",
        "clusterrole-postgres-pod.yaml",
        "deployment.yaml",
    ]:
        assert any(
            item["Target"] == dependency_path + filename
            and item["MisconfSummary"]["Successes"] > 0
            for item in report["Results"]
        ), filename
    result, report = _trivy_scan(tmp_path, ignore_exceptions=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _trivy_failures(report) == {
        (dependency_path + "clusterrole-postgres-pod.yaml", "KSV-0056"),
        (dependency_path + "clusterrole.yaml", "KSV-0041"),
        (dependency_path + "clusterrole.yaml", "KSV-0053"),
        (dependency_path + "clusterrole.yaml", "KSV-0056"),
        (dependency_path + "deployment.yaml", "KSV-0118"),
    }
    # Same basename and chart, but our wrapper's own template is not exempted.
    (chart / "templates/clusterrole.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": "first-party-control"},
                "rules": [
                    {
                        "apiGroups": [""],
                        "resources": ["secrets", "pods/exec", "services"],
                        "verbs": ["create", "get", "update"],
                    }
                ],
            }
        )
    )
    # A nearby first-party workload with the same missing pod context must fail.
    (chart / "templates/deployment.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "first-party-control"},
                "spec": {
                    "containers": [
                        {
                            "name": "control",
                            "image": "busybox:1.37.0",
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 1000,
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ]
                },
            }
        )
    )
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _trivy_failures(report) == {
        ("charts/postgres-operator/templates/clusterrole.yaml", "KSV-0041"),
        ("charts/postgres-operator/templates/clusterrole.yaml", "KSV-0053"),
        ("charts/postgres-operator/templates/clusterrole.yaml", "KSV-0056"),
        ("charts/postgres-operator/templates/deployment.yaml", "KSV-0118"),
    }


def test_trivy_operator_other_security_checks_remain_enforcing(tmp_path: Path) -> None:
    chart = _copy_chart(tmp_path, "postgres-operator")
    values = yaml.safe_load((chart / "values.yaml").read_text())
    values["postgres-operator"]["securityContext"]["privileged"] = True
    (chart / "values.yaml").write_text(yaml.safe_dump(values))
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _trivy_failures(report) == {
        (
            "charts/postgres-operator/charts/postgres-operator/templates/deployment.yaml",
            "KSV-0017",
        )
    }


def test_trivy_lifecycle_skip_does_not_hide_platform_workloads(tmp_path: Path) -> None:
    chart = _copy_chart(tmp_path, "platform")
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    target = "charts/platform/templates/frontend.yaml"
    assert any(
        item["Target"] == target and item["MisconfSummary"]["Successes"] > 0
        for item in report["Results"]
    )
    assert not any(
        item["Target"] == "charts/platform/templates/lifecycle-contract.yaml"
        for item in report["Results"]
    )
    frontend = chart / "templates/frontend.yaml"
    original = frontend.read_text()
    assert original.count("allowPrivilegeEscalation: false") == 1
    frontend.write_text(
        original.replace(
            "allowPrivilegeEscalation: false",
            "allowPrivilegeEscalation: false\n            privileged: true",
        )
    )
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _trivy_failures(report) == {(target, "KSV-0017")}


def test_hook_installation_and_whole_project_gates() -> None:
    config = tomllib.loads((ROOT / "prek.toml").read_text())
    assert set(config["default_install_hook_types"]) == {"pre-commit", "pre-push"}
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    assert all(
        "priority" not in hook for hook in hooks.values()
    )  # Prek defaults serialize mutators and readers.
    assert "mypy" not in hooks
    assert hooks["typecheck"]["entry"] == "make typecheck"
    assert hooks["typecheck"]["language"] == "system"
    assert hooks["typecheck"]["pass_filenames"] is False
    assert hooks["typecheck"]["always_run"] is True
    assert hooks["kube-linter"]["entry"] == "make lint-kubernetes"
    assert hooks["kube-linter"]["types"] == ["file"]
    assert re.search(
        hooks["kube-linter"]["files"], "charts/platform/templates/_helpers.tpl"
    )
    assert re.search(hooks["kube-linter"]["files"], "scripts/render_profiles.py")
    assert re.search(
        hooks["kubeconform"]["files"], ".schemas/acid.zalan.do/postgresql_v1.json"
    )
    assert (
        hooks["gitleaks"]["entry"] == "gitleaks dir --redact --config .gitleaks.toml ."
    )
    assert hooks["gitleaks"]["always_run"] is True
    for name in [
        "gitleaks",
        "checkov",
        "kube-linter",
        "helm-lint",
        "check-helm-deps",
        "kubeconform",
        "trivy-fs",
    ]:
        assert hooks[name]["pass_filenames"] is False, name
    formatter = next(
        repo for repo in config["repos"] if repo["repo"].endswith("/ruff-pre-commit")
    )
    assert formatter["rev"].startswith("v")
    assert "ruff-format" in {hook["id"] for hook in formatter["hooks"]}


def test_profile_renderer_preserves_raw_outputs_and_uses_chart_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    projection = "temporal:\n  serviceAccount:\n    name: chart-derived\n"

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        assert kwargs["check"] is True
        if arguments[:3] == ["helm", "show", "chart"]:
            artifact = Path(arguments[3])
            dependency = next(
                dependency
                for dependency in render_profiles.validate_lock(artifact.parent.parent)
                if artifact.name == f"{dependency['name']}-{dependency['version']}.tgz"
            )
            metadata = yaml.safe_dump(
                {"name": dependency["name"], "version": dependency["version"]}
            )
            return subprocess.CompletedProcess(arguments, 0, stdout=metadata)
        if "bootstrap.enabled=true" in arguments:
            document = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "labels": {"app.kubernetes.io/component": "lifecycle-contract"}
                },
                "data": {"temporal-values.yaml": projection},
            }
        else:
            document = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "raw-output"},
            }
        content = "# Native Helm output, do not rewrite\n" + yaml.safe_dump(document)
        return subprocess.CompletedProcess(arguments, 0, stdout=content)

    monkeypatch.setattr(render_profiles.subprocess, "run", run)
    render_profiles.render_profiles(
        ROOT, tmp_path, "custom-release", "custom-namespace"
    )
    assert len(calls) == 8
    assert calls[0][:3] == ["helm", "show", "chart"]
    assert {path.name for path in tmp_path.iterdir()} == set(
        render_profiles.PROFILE_FILES
    ) | {"temporal-values.yaml"}
    assert (tmp_path / "temporal-values.yaml").read_text() == projection
    full = next(call for call in calls if "--is-upgrade" in call)
    assert "temporal.enabled=true" in full and "workers.enabled=true" in full
    assert "bootstrap.prerequisitesReady=true" in full
    assert full[full.index("-f") + 1] == str(tmp_path / "temporal-values.yaml")
    assert (
        full[2] == "custom-release"
        and full[full.index("--namespace") + 1] == "custom-namespace"
    )
    for filename in render_profiles.PROFILE_FILES:
        path = tmp_path / filename
        assert path.read_text().startswith("# Native Helm output, do not rewrite\n")
        assert path.stat().st_mode & 0o777 == 0o600


def _scan_fixture(directory: Path) -> dict[str, str]:
    scripts = directory / "scripts"
    scripts.mkdir()
    for name in ["local-container-scan.sh", "run-all-local-scans.sh"]:
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    image = directory / "images/test-component"
    image.mkdir(parents=True)
    (image / "Dockerfile").write_text("FROM scratch\n")
    binaries = directory / "bin"
    binaries.mkdir()
    uv = binaries / "uv"
    uv.write_text("""#!/usr/bin/env bash
set -eu
case "$*" in
  *"scripts/images.py --format tsv"*)
    if [ "${FAKE_INVENTORY_EXIT:-0}" != '0' ]; then exit "$FAKE_INVENTORY_EXIT"; fi
    if [ -n "${FAKE_IMAGE_INVENTORY:-}" ]; then printf '%s\\n' "$FAKE_IMAGE_INVENTORY";
    else printf 'test-component\\timages/test-component/Dockerfile\\t\\n'; fi ;;
  *"image trivy"*) echo 'aquasec/trivy:test' ;;
  *"build-args --format docker"*) echo '--build-arg=PYTHON_VERSION=3.14.0' ;;
  *) exit 99 ;;
esac
""")
    docker = binaries / "docker"
    docker.write_text("""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
arguments=$(mktemp "$FAKE_DOCKER_STATE/../argv.XXXXXXXX")
printf '%s\\0' "$@" > "$arguments"
printf '%s\\n' "$arguments" >> "$FAKE_DOCKER_ARGUMENTS"
state="$FAKE_DOCKER_STATE"
if [ "$1 $2" = 'buildx build' ]; then
  owner='' tag=''
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --label) owner=${2#*=}; shift ;;
      -t) tag=$2; shift ;;
    esac
    shift
  done
  if [ "${FAKE_HOLD_BUILD:-0}" = '1' ]; then
    sleep 60 & hold=$!
    trap 'kill "$hold"; wait "$hold" 2>/dev/null || true; printf "%s\\n" "$owner" > "$state/$tag"; exit 143' TERM
    touch "$FAKE_DOCKER_READY"
    wait "$hold"
  fi
  printf '%s\\n' "${FAKE_IMAGE_OWNER:-$owner}" > "$state/$tag"
  exit "${FAKE_BUILD_EXIT:-0}"
elif [ "$1 $2" = 'image inspect' ]; then
  tag=${!#}
  if [ "${FAKE_INSPECT_EXIT:-0}" != '0' ]; then echo 'Cannot connect to the Docker daemon' >&2; exit "$FAKE_INSPECT_EXIT"; fi
  if [ -f "$state/$tag" ]; then read -r owner < "$state/$tag"; echo "$owner";
  else echo "Error: No such image: $tag" >&2; exit 1; fi
elif [ "$1 $2" = 'image rm' ]; then
  if [ "${FAKE_IMAGE_REMOVE_EXIT:-0}" != '0' ]; then exit "$FAKE_IMAGE_REMOVE_EXIT"; fi
  rm "$state/$3"
elif [ "$1" = 'inspect' ]; then
  container=${!#}
  if [ "${FAKE_INSPECT_EXIT:-0}" != '0' ]; then echo 'Cannot connect to the Docker daemon' >&2; exit "$FAKE_INSPECT_EXIT"; fi
  if [ -f "$state/$container" ]; then read -r owner < "$state/$container"; echo "$owner";
  else echo "Error: No such object: $container" >&2; exit 1; fi
elif [ "$1" = 'rm' ]; then
  if [ "${FAKE_CONTAINER_REMOVE_EXIT:-0}" != '0' ]; then exit "$FAKE_CONTAINER_REMOVE_EXIT"; fi
  rm "$state/${!#}"
elif [ "$1" = 'run' ]; then
  arguments="$*" owner='' container='' exit_code=''
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --label) owner=${2#*=}; shift ;;
      --name) container=$2; shift ;;
      --exit-code) exit_code=$2; shift ;;
    esac
    shift
  done
  if [ "${FAKE_HOLD_SCAN:-0}" = '1' ]; then
    sleep 60 & hold=$!
    trap 'kill "$hold"; wait "$hold" 2>/dev/null || true; printf "%s\\n" "$owner" > "$state/$container"; exit 143' TERM
    touch "$FAKE_DOCKER_READY"
    wait "$hold"
  fi
  printf '%s\\n' "${FAKE_CONTAINER_OWNER:-$owner}" > "$state/$container"
  case "$arguments" in
    *"--severity HIGH,CRITICAL"*) exit "${FAKE_APP_EXIT:-0}" ;;
    *"--severity HIGH"*)
      if [ "${FAKE_OS_HIGH_FINDING:-0}" = '1' ]; then echo 'OS HIGH warning finding'; exit "$exit_code"; fi
      exit "${FAKE_WARNING_EXIT:-0}" ;;
    *"--severity CRITICAL"*) exit "${FAKE_OS_EXIT:-0}" ;;
    *) exit 98 ;;
  esac
else
  exit 97
fi
""")
    for executable in [uv, docker]:
        executable.chmod(0o755)
    state = directory / ".cache/mock-docker"
    state.mkdir(parents=True)
    return {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "PLATFORM_MISE_ACTIVE": "1",
        "PLATFORM_ROOT": str(directory),
        "FAKE_DOCKER_LOG": str(directory / ".cache/docker.log"),
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_DOCKER_ARGUMENTS": str(directory / ".cache/argument-index"),
        "FAKE_DOCKER_READY": str(directory / ".cache/docker-ready"),
    }


def _docker_calls(environment: dict[str, str]) -> list[list[str]]:
    return [
        [
            argument.decode()
            for argument in Path(filename).read_bytes().split(b"\0")[:-1]
        ]
        for filename in Path(environment["FAKE_DOCKER_ARGUMENTS"])
        .read_text()
        .splitlines()
    ]


def _assert_image_policy(environment: dict[str, str]) -> None:
    scans = [call for call in _docker_calls(environment) if call[0] == "run"]
    assert len(scans) == 3
    for call, package_type, severity, exit_code in zip(
        scans,
        ["library", "os", "os"],
        ["HIGH,CRITICAL", "HIGH", "CRITICAL"],
        ["1", "0", "1"],
        strict=True,
    ):
        assert call[call.index("--pkg-types") + 1] == package_type
        assert call[call.index("--severity") + 1] == severity
        assert call[call.index("--exit-code") + 1] == exit_code
        assert call[call.index("--config") + 1] == ""
        assert call[call.index("--scanners") + 1] == "vuln,secret"
        assert "--ignore-unfixed" not in call


@pytest.mark.parametrize(
    "failure", ["FAKE_APP_EXIT", "FAKE_WARNING_EXIT", "FAKE_OS_EXIT"]
)
def test_image_scan_captures_each_failure_and_removes_only_owned_tag(
    tmp_path: Path, failure: str
) -> None:
    environment = _scan_fixture(tmp_path)
    environment[failure] = "7"
    result = subprocess.run(
        [
            "bash",
            str(tmp_path / "scripts/local-container-scan.sh"),
            "images/test-component",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    commands = Path(environment["FAKE_DOCKER_LOG"]).read_text().splitlines()
    scans = [command for command in commands if command.startswith("run ")]
    assert len(scans) == 3  # set -e must not stop after the first enforcing failure.
    _assert_image_policy(environment)
    assert all(
        "--config " in command and "--scanners vuln,secret" in command
        for command in scans
    )
    assert all("--ignore-unfixed" not in command for command in scans)
    assert all(
        str(tmp_path / ".cache/trivy") + ":/root/.cache/trivy" in command
        for command in scans
    )
    assert any(command.startswith("image rm local-scan:") for command in commands)
    assert not list(Path(environment["FAKE_DOCKER_STATE"]).iterdir())
    status = next((tmp_path / ".cache/local-scans").glob("test-component.*/status"))
    assert status.read_text().strip() == "1"


def test_image_build_failure_still_cleans_owned_loaded_image(tmp_path: Path) -> None:
    environment = _scan_fixture(tmp_path)
    environment["FAKE_BUILD_EXIT"] = "42"
    result = subprocess.run(
        [
            "bash",
            str(tmp_path / "scripts/local-container-scan.sh"),
            "images/test-component",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 42, result.stdout + result.stderr
    commands = Path(environment["FAKE_DOCKER_LOG"]).read_text().splitlines()
    assert not any(command.startswith("run ") for command in commands)
    assert any(command.startswith("image rm local-scan:") for command in commands)
    status = next((tmp_path / ".cache/local-scans").glob("test-component.*/status"))
    assert status.read_text().strip() == "42"


def test_scan_runner_records_failure_without_deleting_other_run_logs(
    tmp_path: Path,
) -> None:
    environment = _scan_fixture(tmp_path)
    environment["FAKE_APP_EXIT"] = "7"
    scans = tmp_path / ".cache/local-scans"
    scans.mkdir()
    previous = scans / "another-run.log"
    previous.write_text("not owned by this invocation\n")
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/run-all-local-scans.sh")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    run_directory = next(scans.glob("run.*"))
    assert (run_directory / "test-component.status").read_text().strip() == "1"
    assert (run_directory / "run.status").read_text().strip() == "1"
    assert "Scan failed" in (run_directory / "test-component.log").read_text()
    assert previous.read_text() == "not owned by this invocation\n"


@pytest.mark.parametrize("has_high_warning", [False, True])
def test_image_policy_success_and_os_high_warning_only(
    tmp_path: Path, has_high_warning: bool
) -> None:
    environment = _scan_fixture(tmp_path)
    if has_high_warning:
        environment["FAKE_OS_HIGH_FINDING"] = "1"
    result = subprocess.run(
        [
            "bash",
            str(tmp_path / "scripts/local-container-scan.sh"),
            "images/test-component",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_image_policy(environment)
    assert not list(Path(environment["FAKE_DOCKER_STATE"]).iterdir())
    if has_high_warning:
        assert "OS HIGH warning finding" in result.stdout


def test_image_cleanup_preserves_resources_with_unowned_labels(tmp_path: Path) -> None:
    environment = _scan_fixture(tmp_path)
    environment.update(FAKE_IMAGE_OWNER="unowned", FAKE_CONTAINER_OWNER="unowned")
    result = subprocess.run(
        [
            "bash",
            str(tmp_path / "scripts/local-container-scan.sh"),
            "images/test-component",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _docker_calls(environment)
    assert not any(call[0] == "rm" or call[:2] == ["image", "rm"] for call in calls)
    remaining = list(Path(environment["FAKE_DOCKER_STATE"]).iterdir())
    assert len(remaining) == 4
    assert all(path.read_text().strip() == "unowned" for path in remaining)


@pytest.mark.parametrize(
    "failure", ["FAKE_CONTAINER_REMOVE_EXIT", "FAKE_IMAGE_REMOVE_EXIT"]
)
def test_image_cleanup_failure_cannot_report_success(
    tmp_path: Path, failure: str
) -> None:
    environment = _scan_fixture(tmp_path)
    environment[failure] = "9"
    result = subprocess.run(
        [
            "bash",
            str(tmp_path / "scripts/local-container-scan.sh"),
            "images/test-component",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Failed to remove owned scan" in result.stdout
    status = next((tmp_path / ".cache/local-scans").glob("test-component.*/status"))
    assert status.read_text().strip() == "1"


def test_cleanup_inspection_failure_is_not_confirmed_absence_or_success(
    tmp_path: Path,
) -> None:
    environment = _scan_fixture(tmp_path)
    environment["FAKE_INSPECT_EXIT"] = "17"
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/local-container-scan.sh"), "test-component"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    calls = _docker_calls(environment)
    assert not any(call[0] == "rm" or call[:2] == ["image", "rm"] for call in calls)
    assert "Cannot verify image cleanup ownership" in result.stdout
    directory = next((tmp_path / ".cache/local-scans").glob("test-component.*"))
    assert (directory / "status").read_text().strip() == "1"
    assert any(
        "Cannot connect to the Docker daemon" in path.read_text()
        for path in directory.glob("inspect-*.log")
    )


def test_scan_inventory_helper_failure_is_fatal_before_docker(tmp_path: Path) -> None:
    environment = _scan_fixture(tmp_path)
    environment["FAKE_INVENTORY_EXIT"] = "43"
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/run-all-local-scans.sh")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 43, result.stdout + result.stderr
    assert not Path(environment["FAKE_DOCKER_LOG"]).exists()


def test_scan_inventory_covers_both_substrate_export_targets(tmp_path: Path) -> None:
    environment = _scan_fixture(tmp_path)
    substrate = tmp_path / "images/agent-substrate"
    substrate.mkdir()
    (substrate / "Dockerfile").write_text(
        "FROM scratch AS ateapi\nFROM scratch AS atecontroller\n"
    )
    environment["FAKE_IMAGE_INVENTORY"] = (
        "ateapi\timages/agent-substrate/Dockerfile\tateapi\n"
        "atecontroller\timages/agent-substrate/Dockerfile\tatecontroller"
    )
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/run-all-local-scans.sh"), "agent-substrate"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    builds = [
        call for call in _docker_calls(environment) if call[:2] == ["buildx", "build"]
    ]
    assert {call[call.index("--target") + 1] for call in builds} == {
        "ateapi",
        "atecontroller",
    }
    assert all(
        call[call.index("-f") + 1] == "images/agent-substrate/Dockerfile"
        for call in builds
    )
    directory = next((tmp_path / ".cache/local-scans").glob("run.*"))
    assert (directory / "ateapi.status").read_text().strip() == "0"
    assert (directory / "atecontroller.status").read_text().strip() == "0"


def _reap_fixture_group(process: subprocess.Popen[str]) -> None:
    # Descendants can retain pipes even after their parent exits. This fresh
    # fixture session/process group never contains unrelated developer processes.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=10)


@pytest.mark.parametrize("stage", ["FAKE_HOLD_BUILD", "FAKE_HOLD_SCAN"])
@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_scan_signal_reaps_producer_before_final_owned_cleanup(
    tmp_path: Path, stage: str, interrupt: signal.Signals
) -> None:
    environment = _scan_fixture(tmp_path)
    environment[stage] = "1"
    process = subprocess.Popen(
        [
            "bash",
            str(tmp_path / "scripts/local-container-scan.sh"),
            "images/test-component",
        ],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not Path(environment["FAKE_DOCKER_READY"]).exists():
            assert process.poll() is None, (
                "Scan terminated before reaching the held producer"
            )
            assert time.monotonic() < deadline, (
                "Producer did not reach the synchronization marker"
            )
            time.sleep(0.01)
        process.send_signal(interrupt)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 128 + interrupt, stdout + stderr
        assert not list(Path(environment["FAKE_DOCKER_STATE"]).iterdir())
        status = next((tmp_path / ".cache/local-scans").glob("test-component.*/status"))
        assert status.read_text().strip() == str(128 + interrupt)
    finally:
        _reap_fixture_group(process)


def test_scan_runner_signal_records_component_and_run_status(tmp_path: Path) -> None:
    environment = _scan_fixture(tmp_path)
    environment["FAKE_HOLD_SCAN"] = "1"
    process = subprocess.Popen(
        ["bash", str(tmp_path / "scripts/run-all-local-scans.sh")],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not Path(environment["FAKE_DOCKER_READY"]).exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 143, stdout + stderr
        assert not list(Path(environment["FAKE_DOCKER_STATE"]).iterdir())
        directory = next((tmp_path / ".cache/local-scans").glob("run.*"))
        assert (directory / "test-component.status").read_text().strip() == "143"
        assert (directory / "run.status").read_text().strip() == "143"
    finally:
        _reap_fixture_group(process)
