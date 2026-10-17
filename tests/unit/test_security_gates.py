"""Security gate controls use nonfunctional values assembled only at runtime."""

import hashlib
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).absolute().parents[2]


def _synthetic_token() -> str:
    prefix = "ghp_"
    return (
        prefix
        + hashlib.sha256(b"nonfunctional scanner positive control").hexdigest()[:36]
    )


def _tool_binary(tool: str) -> str:
    executable = shutil.which(tool)
    if executable is not None and Path(executable).is_file():
        return executable
    result = subprocess.run(
        ["mise", "exec", "--", "which", tool],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    resolved = result.stdout.strip()
    assert Path(resolved).is_file(), result.stdout + result.stderr
    return resolved


@pytest.fixture(scope="session")
def gitleaks_binary() -> str:
    return _tool_binary("gitleaks")


@pytest.fixture(scope="session")
def kube_linter_binary() -> str:
    return _tool_binary("kube-linter")


def _scan_git(
    directory: Path, executable: str, *, staged: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    args = [
        executable,
        "git",
        "--redact",
        "--config",
        str(ROOT / ".gitleaks.toml"),
        "--report-format",
        "json",
        "--report-path",
        "report.json",
    ]
    if staged:
        args.extend(["--pre-commit", "--staged"])
    else:
        args.extend(["--log-opts=HEAD"])
    result = subprocess.run(
        args,
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
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("token = " + _synthetic_token() + "\n")
    subprocess.run(["git", "add", filename], cwd=tmp_path, check=True)
    result, findings = _scan_git(tmp_path, gitleaks_binary, staged=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert any(
        finding["RuleID"] == "github-pat" and finding["File"].endswith(filename)
        for finding in findings
    )


def test_gitleaks_git_mode_ignores_untracked_and_gitignored_files(
    tmp_path: Path, gitleaks_binary: str
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text(".cache/\n.venv/\nnode_modules/\n")
    (tmp_path / ".secrets.baseline").write_text("token = " + _synthetic_token() + "\n")
    for untracked in (".cache/test.env", ".venv/config.txt", "untracked.txt"):
        p = tmp_path / untracked
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("token = " + _synthetic_token() + "\n")
    subprocess.run(
        ["git", "add", ".gitignore", ".secrets.baseline"], cwd=tmp_path, check=True
    )
    result, findings = _scan_git(tmp_path, gitleaks_binary, staged=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert findings == []
    (tmp_path / "first-party.txt").write_text("token = " + _synthetic_token() + "\n")
    subprocess.run(["git", "add", "first-party.txt"], cwd=tmp_path, check=True)
    result, findings = _scan_git(tmp_path, gitleaks_binary, staged=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert any(finding["File"].endswith("first-party.txt") for finding in findings)


def test_scanner_policy_preserves_complementary_enforcement() -> None:
    gitleaks = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert gitleaks["extend"]["useDefault"] is True
    assert not gitleaks["extend"].get("disabledRules")
    assert len(gitleaks["allowlists"]) == 1
    baseline_regex = gitleaks["allowlists"][0]["paths"][0]
    assert re.fullmatch(baseline_regex, ".secrets.baseline")
    assert not re.search(baseline_regex, ".secrets.baseline.backup")
    assert not re.search(baseline_regex, "source/.secrets.baseline")

    kube_linter = yaml.safe_load((ROOT / ".kube-linter.yaml").read_text())["checks"]
    assert kube_linter["addAllBuiltIn"] is True
    excluded = set(kube_linter.get("exclude") or ())
    assert {
        "no-readiness-probe",
        "no-liveness-probe",
        "run-as-non-root",
        "no-read-only-root-fs",
        "privilege-escalation-container",
        "privileged-container",
    }.isdisjoint(excluded)
    assert not (ROOT / ".kube-linter-operator.yaml").exists()
    for template in (ROOT / "charts").glob("*/templates/*"):
        if template.is_file():
            assert "ignore-check.kube-linter.io/all" not in template.read_text(), (
                template
            )

    trivy = yaml.safe_load((ROOT / "trivy.yaml").read_text())
    assert set(trivy["scan"]["scanners"]) == {"vuln", "misconfig", "secret"}
    assert set(trivy["severity"]) == {"HIGH", "CRITICAL"}
    assert trivy["vulnerability"]["ignore-unfixed"] is True
    assert set(trivy["pkg-types"]) == {"os", "library"}
    assert trivy["exit-code"] == 1
    assert trivy["cache-dir"] == ".cache/trivy"
    assert "db" not in trivy


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
    helm = _tool_binary("helm")
    path = tmp_path / "kind.yaml"
    rendered = subprocess.run(
        [
            helm,
            "template",
            "platform",
            str(ROOT / "charts/platform"),
            "--namespace",
            "platform",
            "-f",
            str(ROOT / "charts/platform/values-kind.yaml"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    path.write_text(rendered.stdout)
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
    assert len(temporal) == 1
    temporal[0]["spec"]["template"]["spec"]["containers"][0].pop("livenessProbe")
    path.write_text(yaml.safe_dump_all(documents))
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "no-liveness-probe" in result.stdout + result.stderr
    assert temporal[0]["metadata"]["name"] in result.stdout + result.stderr


def _trivy_scan(
    directory: Path,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    executable = _tool_binary("trivy")
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
        "--scanners",
        "misconfig,secret",
        "--cache-dir",
        str(ROOT / ".cache/trivy"),
        "--include-non-failures",
        "--format",
        "json",
    ]
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
    rule = '  resources: ["pods"]\n  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]'
    assert original.count(rule) == 1
    rbac.write_text(
        original.replace(rule, rule.replace('["pods"]', '["pods", "services"]'))
    )
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _trivy_failures(report) == {(target, "KSV-0056")}


def test_trivy_platform_workloads_scanned(tmp_path: Path) -> None:
    chart = _copy_chart(tmp_path, "platform")
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    target = "charts/platform/templates/frontend.yaml"
    assert any(
        item["Target"] == target and item["MisconfSummary"]["Successes"] > 0
        for item in report["Results"]
    )
    frontend = chart / "templates/frontend.yaml"
    original = frontend.read_text()
    frontend.write_text(
        original.replace(
            "allowPrivilegeEscalation: false",
            "allowPrivilegeEscalation: false\n            privileged: true",
        )
    )
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _trivy_failures(report) == {(target, "KSV-0017")}


def test_hook_installation_and_parallel_priority_tiers() -> None:
    config = tomllib.loads((ROOT / "prek.toml").read_text())
    assert set(config["default_install_hook_types"]) == {"pre-commit", "pre-push"}
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    assert hooks["trailing-whitespace"]["priority"] == 0
    assert hooks["end-of-file-fixer"]["priority"] == 0
    assert hooks["actions-lock"]["priority"] == 0
    assert hooks["actions-lock"]["entry"] == "make actions-lock"
    assert hooks["check-helm-deps"]["priority"] == 0
    assert hooks["check-helm-deps"]["entry"] == "make check-helm-deps"
    assert hooks["ruff"]["priority"] == 0
    assert hooks["ruff"]["entry"] == "mise exec -- ruff check --fix"
    assert hooks["ruff-format"]["priority"] == 1
    assert hooks["ruff-format"]["entry"] == "mise exec -- ruff format"
    assert hooks["actionlint"]["entry"] == "mise exec -- actionlint"
    assert hooks["hadolint"]["entry"] == "mise exec -- hadolint"
    assert hooks["typecheck"]["entry"] == "make typecheck"
    assert hooks["gitleaks"]["entry"] == "make scan-secrets-staged"
    assert hooks["pytest"]["entry"] == "make pytest-unit"
    for name in [
        "gitleaks",
        "checkov",
        "kube-linter",
        "helm-lint",
        "kubeconform",
        "trivy-fs",
        "pytest",
    ]:
        assert hooks[name]["pass_filenames"] is False, name
        assert hooks[name]["priority"] == 1, name
        assert hooks[name]["entry"].startswith("make "), name
