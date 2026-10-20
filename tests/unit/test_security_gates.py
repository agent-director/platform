"""Security gate controls use nonfunctional values assembled only at runtime."""

import functools
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


@functools.cache
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
    skip_dirs = set(trivy["scan"]["skip-dirs"])
    assert {
        "hack",
        ".claude",
        ".nx",
        "graphify-out",
        "apps/ui/node_modules",
        "apps/ui/.cache",
        "apps/ui/.next",
    } <= skip_dirs
    assert not any(str(entry).startswith("frontend/") for entry in skip_dirs)


def test_ui_workspace_and_ignore_files_enforce_relocated_paths() -> None:
    pnpm_ws = yaml.safe_load((ROOT / "apps/ui/pnpm-workspace.yaml").read_text())
    assert pnpm_ws["packages"] == ["."]
    assert pnpm_ws["strictDepBuilds"] is True
    assert pnpm_ws["minimumReleaseAge"] == 2880
    assert pnpm_ws["allowBuilds"] == {"nx": False, "nx-mcp": False, "msw": False}

    gitignore_lines = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert {
        "hack/",
        ".nx/",
        "graphify-out/",
        "apps/ui/.next/",
        "apps/ui/next-env.d.ts",
        "apps/ui/tsconfig.tsbuildinfo",
    } <= gitignore_lines
    assert not any(line.startswith("frontend/") for line in gitignore_lines)

    dockerignore_lines = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert {".claude", ".nx/", "graphify-out/", "hack"} <= dockerignore_lines
    assert not any(line.startswith("frontend/") for line in dockerignore_lines)

    env_example = (ROOT / ".env.example").read_text()
    assert (
        'GITHUB_CONFIG_PATH="apps/core/unified_api/litellm_config.yaml"' in env_example
    )
    assert 'GITHUB_CONFIG_PATH="unified_api/litellm_config.yaml"' not in env_example


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
    clean = tmp_path / "clean" / "Dockerfile"
    clean.parent.mkdir()
    clean.write_text("FROM alpine:3.20.0\nUSER 1000\nHEALTHCHECK CMD /bin/true\n")
    violating = tmp_path / "violating" / "Dockerfile"
    violating.parent.mkdir()
    violating.write_text("FROM alpine:3.20.0\nHEALTHCHECK CMD /bin/true\n")
    result, report = _trivy_scan(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert any(
        item["Target"] == "clean/Dockerfile" and item["MisconfSummary"]["Successes"] > 0
        for item in report["Results"]
    )
    assert _trivy_failures(report) == {("violating/Dockerfile", "DS-0002")}


@pytest.mark.parametrize("case", ["clean", "remove_ignore", "broaden_resources"])
def test_trivy_substrate_inline_exception_does_not_hide_adjacent_rule(
    tmp_path: Path, case: str
) -> None:
    chart = _copy_chart(tmp_path, "agent-substrate")
    target = "charts/agent-substrate/templates/rbac.yaml"
    rbac = chart / "templates/rbac.yaml"
    original = rbac.read_text()
    assert original.count("# trivy:ignore:KSV-0056") == 1
    rule = '  resources: ["pods"]\n  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]'
    assert original.count(rule) == 1
    if case == "remove_ignore":
        rbac.write_text(original.replace("# trivy:ignore:KSV-0056\n", ""))
    elif case == "broaden_resources":
        rbac.write_text(
            original.replace(rule, rule.replace('["pods"]', '["pods", "services"]'))
        )
    result, report = _trivy_scan(tmp_path)
    if case == "clean":
        assert result.returncode == 0, result.stdout + result.stderr
        assert any(
            item["Target"] == target and item["MisconfSummary"]["Successes"] > 0
            for item in report["Results"]
        )
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert _trivy_failures(report) == {(target, "KSV-0056")}


def test_trivy_platform_workloads_scanned(tmp_path: Path) -> None:
    chart = _copy_chart(tmp_path, "platform")
    target = "charts/platform/templates/frontend.yaml"
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
    assert any(
        item["Target"] == target and item["MisconfSummary"]["Successes"] > 0
        for item in report["Results"]
    )
    assert _trivy_failures(report) == {(target, "KSV-0017")}


def test_hook_installation_and_parallel_priority_tiers() -> None:
    config = tomllib.loads((ROOT / "prek.toml").read_text())
    assert set(config["default_install_hook_types"]) == {"pre-commit", "pre-push"}
    assert config["default_stages"] == ["pre-commit"]
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
    assert (
        hooks["biome"]["entry"]
        == "mise exec -- biome check --config-path apps/ui/biome.json apps/ui/src"
    )
    assert hooks["biome"]["priority"] == 1
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


def test_nx_workspace_biome_and_project_graph_descriptors() -> None:
    mise_cfg = tomllib.loads((ROOT / "mise.toml").read_text())
    assert mise_cfg["tools"]["biome"] == "2.5.15"
    assert mise_cfg["env"]["NX_NO_CLOUD"] == "true"
    assert mise_cfg["env"]["NX_DAEMON"] == "false"
    assert mise_cfg["env"]["NX_CACHE_DIRECTORY"] == "{{ config_root }}/.cache/nx"

    nx_cfg = json.loads((ROOT / "nx.json").read_text())
    assert nx_cfg["cacheDirectory"] == ".cache/nx"
    assert set(nx_cfg["targetDefaults"]) >= {"lint", "typecheck", "test", "build"}
    for target in ("lint", "typecheck", "test", "build"):
        assert nx_cfg["targetDefaults"][target]["cache"] is True
    assert "^default" in nx_cfg["targetDefaults"]["typecheck"]["inputs"]
    assert "^default" in nx_cfg["targetDefaults"]["test"]["inputs"]

    nxignore = {
        line.strip()
        for line in (ROOT / ".nxignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert {"hack/", ".cache/", ".venv/", "graphify-out/", ".schemas/"} <= nxignore

    biome_cfg = json.loads((ROOT / "apps/ui/biome.json").read_text())
    assert biome_cfg["linter"]["enabled"] is True
    assert biome_cfg["linter"]["rules"]["recommended"] is True
    assert biome_cfg["linter"]["rules"]["security"]["recommended"] is True

    project_paths = {
        "contracts": "libs/contracts/project.json",
        "core": "apps/core/project.json",
        "sandbox-exec": "apps/sandbox-exec/project.json",
        "ui": "apps/ui/project.json",
        "charts": "charts/project.json",
        "scripts": "tools/scripts/project.json",
        "repo-tests": "tests/project.json",
    }
    basetemps: set[str] = set()
    for expected_name, rel_path in project_paths.items():
        proj = json.loads((ROOT / rel_path).read_text())
        assert proj["name"] == expected_name
        if "test" in proj.get("targets", {}):
            cmd = proj["targets"]["test"]["command"]
            assert "--no-sync" in cmd
            match = re.search(r"--basetemp=(\S+)", cmd)
            assert match is not None, f"Missing --basetemp in {rel_path}"
            basetemps.add(match.group(1))
    assert len(basetemps) == 4

    ui_proj = json.loads((ROOT / "apps/ui/project.json").read_text())
    assert ui_proj["targets"]["build"]["dependsOn"] == ["typecheck"]
    assert "{projectRoot}/.next/types/**" in ui_proj["targets"]["typecheck"]["outputs"]

    charts_proj = json.loads((ROOT / "charts/project.json").read_text())
    assert {
        "{workspaceRoot}/Makefile",
        "{workspaceRoot}/.kube-linter.yaml",
        "{workspaceRoot}/.checkov.yaml",
        "{workspaceRoot}/.schemas/**/*",
    } <= set(charts_proj["targets"]["lint"]["inputs"])

    repo_tests_proj = json.loads((ROOT / "tests/project.json").read_text())
    assert {
        "{workspaceRoot}/Makefile",
        "{workspaceRoot}/prek.toml",
        "{workspaceRoot}/nx.json",
        "{workspaceRoot}/.nxignore",
        "{workspaceRoot}/.graphifyignore",
        "{workspaceRoot}/trivy.yaml",
        "{workspaceRoot}/.trivyignore.yaml",
        "{workspaceRoot}/.gitleaks.toml",
        "{workspaceRoot}/.kube-linter.yaml",
        "{workspaceRoot}/.checkov.yaml",
        "{workspaceRoot}/images/**/*",
        "{workspaceRoot}/.github/workflows/**/*",
    } <= set(repo_tests_proj["targets"]["test"]["inputs"])


def test_graphify_ignore_makefile_and_workflow_security() -> None:
    graphify_ignore = {
        line.strip()
        for line in (ROOT / ".graphifyignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert {
        "hack/**",
        ".env*",
        ".secrets.baseline",
        ".cache/**",
        ".venv/**",
        "**/node_modules/**",
        ".nx/**",
        "graphify-out/**",
        "charts/*/charts/**",
        ".schemas/**",
        "apps/ui/.next/**",
    } <= graphify_ignore

    makefile = (ROOT / "Makefile").read_text()
    assert "graphify:" in makefile
    assert 'uvx --from "graphifyy==0.9.92" graphify extract . --code-only' in makefile
    assert (
        'uvx --from "graphifyy==0.9.92" graphify cluster-only . --no-viz --no-label'
        in makefile
    )
    assert "GRAPHIFY_BACKEND=none" in makefile

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/graphify-brain-refresh.yaml").read_text()
    )
    assert workflow["concurrency"] == {
        "group": "graphify-brain-refresh",
        "cancel-in-progress": False,
    }
    assert workflow["permissions"] == {}
    job = workflow["jobs"]["refresh-brain"]
    assert job["permissions"] == {"contents": "write"}
    checkout_step = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout_step["with"]["persist-credentials"] is False


def test_hack_shadow_repository_gitignore_and_scratch_cleanup() -> None:
    hack_dir = ROOT / "hack"
    if not hack_dir.is_dir():
        pytest.skip("hack/ shadow context is not present in this checkout")
    hack_gitignore = hack_dir / ".gitignore"
    assert hack_gitignore.is_file()
    ignored_entries = {
        line.strip()
        for line in hack_gitignore.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert {
        "worktrees/",
        "repos/",
        "extracted-repos/",
        "*.bak*",
        "*.log",
        ".DS_Store",
    } <= ignored_entries
    assert not (hack_dir / "findings.json").exists()


def test_monorepo_documentation_scopes_and_roadmap_paths() -> None:
    contributing = (ROOT / "CONTRIBUTING.md").read_text()
    for scope in (
        "workspace",
        "layout",
        "nx",
        "dx",
        "core",
        "ui",
        "sandbox-exec",
        "contracts",
    ):
        assert f"- `{scope}`:" in contributing

    readme = (ROOT / "README.md").read_text()
    for module_path in (
        "apps/core",
        "apps/ui",
        "apps/sandbox-exec",
        "libs/contracts",
        "tools/scripts",
    ):
        assert module_path in readme

    roadmap_path = ROOT / "hack/plans/ROADMAP.md"
    if roadmap_path.is_file():
        roadmap = roadmap_path.read_text()
        assert (
            re.search(r"agent-director/(core|ui|sandbox-exec|charts)", roadmap) is None
        )
