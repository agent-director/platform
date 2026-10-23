"""Security gate controls use nonfunctional values assembled only at runtime."""

import functools
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
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
    pnpm_ws = yaml.safe_load((ROOT / "pnpm-workspace.yaml").read_text())
    assert pnpm_ws["packages"] == ["apps/ui"]
    assert pnpm_ws["strictDepBuilds"] is True
    assert pnpm_ws["minimumReleaseAge"] == 2880
    assert pnpm_ws["allowBuilds"] == {"nx": False, "nx-mcp": False, "msw": False}
    assert not (ROOT / "apps/ui/pnpm-workspace.yaml").exists()
    assert not (ROOT / "apps/ui/pnpm-lock.yaml").exists()
    assert (ROOT / "pnpm-lock.yaml").is_file()

    root_pkg = json.loads((ROOT / "package.json").read_text())
    assert "nx" in root_pkg["devDependencies"]
    assert "nx-mcp" in root_pkg["devDependencies"]
    ui_pkg = json.loads((ROOT / "apps/ui/package.json").read_text())
    assert "nx" not in ui_pkg.get("devDependencies", {})
    assert "nx-mcp" not in ui_pkg.get("devDependencies", {})

    gitignore_lines = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert {
        "hack/",
        ".claude/",
        ".mcp.json",
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
    assert {
        ".claude",
        ".mcp.json",
        ".nx/",
        "graphify-out/",
        "hack",
    } <= dockerignore_lines
    assert not any(line.startswith("frontend/") for line in dockerignore_lines)

    env_example = (ROOT / ".env.example").read_text()
    assert (
        'GITHUB_CONFIG_PATH="charts/platform/files/litellm_config.yaml"' in env_example
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
    assert {
        "hack/",
        ".claude/",
        ".mcp.json",
        ".cache/",
        ".venv/",
        "graphify-out/",
        "charts/*/charts/",
    } <= nxignore
    assert ".schemas/" not in nxignore

    biome_cfg = json.loads((ROOT / "apps/ui/biome.json").read_text())
    assert biome_cfg["root"] is False
    assert biome_cfg["linter"]["enabled"] is True
    assert biome_cfg["linter"]["rules"]["recommended"] is True
    assert biome_cfg["linter"]["rules"]["security"]["recommended"] is True
    ui_pkg = json.loads((ROOT / "apps/ui/package.json").read_text())
    assert ui_pkg["scripts"]["lint"] == "biome check --config-path biome.json src"
    assert ui_pkg["scripts"]["test"] == "node --test src/mocks/handlers.test.ts"

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
        assert "apps/ui/node_modules" not in proj["$schema"]
        if expected_name != "ui" and "test" in proj.get("targets", {}):
            cmd = proj["targets"]["test"]["command"]
            assert "--no-sync" in cmd
            match = re.search(r"--basetemp=(\S+)", cmd)
            assert match is not None, f"Missing --basetemp in {rel_path}"
            basetemps.add(match.group(1))
    assert len(basetemps) == 4

    ui_proj = json.loads((ROOT / "apps/ui/project.json").read_text())
    assert ui_proj["targets"]["build"]["dependsOn"] == ["typecheck"]
    assert "{projectRoot}/.next/types/**" in ui_proj["targets"]["typecheck"]["outputs"]
    assert (
        ui_proj["targets"]["test"]["command"]
        == "mise exec -- pnpm --filter platform-ui run test"
    )

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
        "{workspaceRoot}/package.json",
        "{workspaceRoot}/pnpm-workspace.yaml",
        "{workspaceRoot}/pnpm-lock.yaml",
        "{workspaceRoot}/nx.json",
        "{workspaceRoot}/.nxignore",
        "{workspaceRoot}/.graphifyignore",
        "{workspaceRoot}/.gitignore",
        "{workspaceRoot}/.dockerignore",
        "{workspaceRoot}/trivy.yaml",
        "{workspaceRoot}/.trivyignore.yaml",
        "{workspaceRoot}/.gitleaks.toml",
        "{workspaceRoot}/.kube-linter.yaml",
        "{workspaceRoot}/.checkov.yaml",
        "{workspaceRoot}/images/**/*",
        "{workspaceRoot}/.github/workflows/**/*",
        "{workspaceRoot}/.schemas/**/*",
    } <= set(repo_tests_proj["targets"]["test"]["inputs"])

    if (ROOT / "hack/ai/mcp.json").is_file():
        mcp_cfg = json.loads((ROOT / "hack/ai/mcp.json").read_text())
        assert mcp_cfg["mcpServers"]["nx-mcp"] == {
            "type": "stdio",
            "command": "mise",
            "args": [
                "exec",
                "--",
                "pnpm",
                "exec",
                "nx-mcp",
                ".",
                "--no-minimal",
                "--disableTelemetry",
            ],
        }
    if (ROOT / "hack/ai/claude-settings.json").is_file():
        claude_settings = json.loads(
            (ROOT / "hack/ai/claude-settings.json").read_text()
        )
        session_hooks = claude_settings["hooks"]["SessionStart"][0]["hooks"]
        assert session_hooks == [
            {
                "type": "command",
                "command": 'bash "${CLAUDE_PROJECT_DIR}/tools/scripts/fetch-graphify-brain.sh"',
                "timeout": 30,
            }
        ]
        pre_tool_hooks = {
            entry["matcher"]: entry["hooks"][0]["command"]
            for entry in claude_settings["hooks"]["PreToolUse"]
        }
        assert pre_tool_hooks == {
            "Bash|Grep": "command -v graphify >/dev/null 2>&1 || exit 0; graphify hook-guard search",
            "Read|Glob": "command -v graphify >/dev/null 2>&1 || exit 0; graphify hook-guard read",
        }


def test_graphify_ignore_makefile_and_workflow_security() -> None:
    graphify_ignore = {
        line.strip()
        for line in (ROOT / ".graphifyignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert {
        "hack/**",
        ".claude/**",
        ".mcp.json",
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

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["dependency-groups"]["graphify"] == ["graphifyy[sql]==0.9.92"]

    makefile = (ROOT / "Makefile").read_text()
    assert (
        "GRAPHIFY := $(RUN) uv run --locked --only-group graphify graphify" in makefile
    )
    assert "GRAPHIFY_BACKEND ?= none" in makefile
    assert "graphify:" in makefile

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


def _write_graphify_bundle(
    bundle_path: Path,
    *,
    graph: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    extra_member_name: str | None = None,
) -> None:
    members: dict[str, bytes] = {
        "graph.json": json.dumps(
            {"nodes": [{"id": "n1"}], "links": []} if graph is None else graph
        ).encode(),
        "metadata.json": json.dumps(
            {
                "source_sha": "deadbeef",
                "graphify_version": "0.9.92",
                "generated_at": "2026-10-19T00:00:00Z",
            }
            if metadata is None
            else metadata
        ).encode(),
    }
    if extra_member_name is not None:
        members[extra_member_name] = b"untrusted"
    with tarfile.open(bundle_path, "w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def _run_fetch_graphify_brain(
    project_dir: Path, bundle_source: Path | None
) -> subprocess.CompletedProcess[str]:
    bin_dir = project_dir.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh_stub = bin_dir / "gh"
    if bundle_source is None:
        gh_stub.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    else:
        gh_stub.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'out=""\n'
            "while [[ $# -gt 0 ]]; do\n"
            '  if [[ "$1" == "--output" ]]; then out="$2"; shift 2; else shift; fi\n'
            "done\n"
            f'cp "{bundle_source}" "$out"\n',
            encoding="utf-8",
        )
    gh_stub.chmod(0o755)
    env = os.environ.copy()
    env["GRAPHIFY_PROJECT_DIR"] = str(project_dir)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(ROOT / "tools/scripts/fetch-graphify-brain.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("case", "should_hydrate"),
    [
        ("valid_exact_version", True),
        ("valid_newer_version", True),
        ("null_version", False),
        ("literal_null_string_version", False),
        ("older_version", False),
        ("invalid_graph_schema", False),
        ("path_traversal_member", False),
        ("download_failure", False),
    ],
)
def test_fetch_graphify_brain_hook_hydrates_valid_bundle_and_fails_open_on_invalid(
    tmp_path: Path, case: str, should_hydrate: bool
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    existing = project_dir / "graphify-out"
    existing.mkdir()
    (existing / "sentinel.txt").write_text("preserve-me", encoding="utf-8")

    bundle_path: Path | None = tmp_path / "source-bundle.tar.gz"
    if case == "valid_exact_version":
        assert bundle_path is not None
        _write_graphify_bundle(bundle_path)
    elif case == "valid_newer_version":
        assert bundle_path is not None
        _write_graphify_bundle(
            bundle_path,
            metadata={
                "source_sha": "deadbeef",
                "graphify_version": "0.10.0",
                "generated_at": "2026-10-19T00:00:00Z",
            },
        )
    elif case == "null_version":
        assert bundle_path is not None
        _write_graphify_bundle(
            bundle_path,
            metadata={
                "source_sha": "deadbeef",
                "graphify_version": None,
                "generated_at": "2026-10-19T00:00:00Z",
            },
        )
    elif case == "literal_null_string_version":
        assert bundle_path is not None
        _write_graphify_bundle(
            bundle_path,
            metadata={
                "source_sha": "deadbeef",
                "graphify_version": "null",
                "generated_at": "2026-10-19T00:00:00Z",
            },
        )
    elif case == "older_version":
        assert bundle_path is not None
        _write_graphify_bundle(
            bundle_path,
            metadata={
                "source_sha": "deadbeef",
                "graphify_version": "0.9.91",
                "generated_at": "2026-10-19T00:00:00Z",
            },
        )
    elif case == "invalid_graph_schema":
        assert bundle_path is not None
        _write_graphify_bundle(
            bundle_path,
            graph={"nodes": "not-an-array", "links": []},
        )
    elif case == "path_traversal_member":
        assert bundle_path is not None
        _write_graphify_bundle(
            bundle_path,
            extra_member_name="../escaped.txt",
        )
    elif case == "download_failure":
        bundle_path = None

    result = _run_fetch_graphify_brain(project_dir, bundle_path)
    assert result.returncode == 0, result.stdout + result.stderr
    staging_root = project_dir / ".cache/graphify-brain"
    assert not staging_root.exists() or list(staging_root.iterdir()) == []
    assert not (project_dir / "escaped.txt").exists()

    if should_hydrate:
        assert (existing / "graph.json").is_file()
        assert (existing / "metadata.json").is_file()
        assert not (existing / "sentinel.txt").exists()
    else:
        assert (existing / "sentinel.txt").read_text(encoding="utf-8") == "preserve-me"


def test_ui_msw_server_and_handlers_match_dashboard_and_unified_api_contracts() -> None:
    result = subprocess.run(
        ["mise", "exec", "--", "pnpm", "--filter", "platform-ui", "run", "test"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


FAKE_SCAN_MISE = r"""#!/bin/bash
set -euo pipefail
[[ "$1" == exec && "$2" == -- ]] || exit 90
shift 2
printf '%s\n' "$*" >> "$FAKE_SCAN_LOG"
cmd="$1"
shift
case "$cmd" in
  jq)
    exec "$REAL_JQ" "$@"
    ;;
  uv)
    exec "$REAL_PYTHON" "$FAKE_REPO_ROOT/tools/scripts/images.py" --format tsv
    ;;
  docker)
    exit 0
    ;;
  trivy)
    subcmd="$1"
    shift
    if [[ "$subcmd" == image ]]; then
      if [[ " $* " == *" --download-db-only "* ]]; then
        mkdir -p .cache/trivy/db
        : > .cache/trivy/db/trivy.db
        exit 0
      fi
      out=""
      while [[ $# -gt 0 ]]; do
        if [[ "$1" == "--output" ]]; then
          out="$2"
          shift 2
        else
          shift
        fi
      done
      cp "$FAKE_TRIVY_REPORT" "$out"
      exit 0
    elif [[ "$subcmd" == convert ]]; then
      exit_code=0
      severities=""
      report_file=""
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --exit-code) exit_code="$2"; shift 2 ;;
          --severity) severities="$2"; shift 2 ;;
          --config|--scanners|--format) shift 2 ;;
          *) report_file="$1"; shift ;;
        esac
      done
      if [[ "$exit_code" != "0" ]]; then
        if "$REAL_JQ" -e --arg sev "$severities" '
          ($sev | split(",")) as $allowed
          | [(.Results // [])[] | ((.Vulnerabilities // [])[], (.Secrets // [])[]) | select(.Severity as $s | $allowed | index($s))]
          | length > 0
        ' "$report_file" >/dev/null; then
          exit "$exit_code"
        fi
      fi
      exit 0
    fi
    exit 91
    ;;
  *)
    exit 92
    ;;
esac
"""


def _write_fake_scan_mise(directory: Path) -> Path:
    script = directory / "fake-mise"
    script.write_text(FAKE_SCAN_MISE, encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.mark.parametrize(
    ("results", "expected_ok"),
    [
        (
            [
                {
                    "Target": "debian",
                    "Class": "os-pkgs",
                    "Vulnerabilities": [
                        {"VulnerabilityID": "CVE-1", "Severity": "HIGH"}
                    ],
                }
            ],
            True,
        ),
        (
            [
                {
                    "Target": "debian",
                    "Class": "os-pkgs",
                    "Vulnerabilities": [
                        {"VulnerabilityID": "CVE-2", "Severity": "CRITICAL"}
                    ],
                }
            ],
            False,
        ),
        (
            [
                {
                    "Target": "Python",
                    "Class": "lang-pkgs",
                    "Vulnerabilities": [
                        {"VulnerabilityID": "CVE-3", "Severity": "HIGH"}
                    ],
                }
            ],
            False,
        ),
    ],
)
def test_makefile_scan_image_splits_os_and_library_severity_gates(
    tmp_path: Path, results: list[dict[str, Any]], expected_ok: bool
) -> None:
    shutil.copy2(ROOT / "Makefile", tmp_path / "Makefile")
    shutil.copy2(ROOT / "trivy.yaml", tmp_path / "trivy.yaml")
    (tmp_path / "tools/scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        ROOT / "tools/scripts/scan-images.sh",
        tmp_path / "tools/scripts/scan-images.sh",
    )
    fake_mise = _write_fake_scan_mise(tmp_path)
    report_path = tmp_path / "fixture-report.json"
    report_path.write_text(json.dumps({"Results": results}), encoding="utf-8")
    log_path = tmp_path / "scan.log"
    env = {
        **os.environ,
        "FAKE_SCAN_LOG": str(log_path),
        "FAKE_TRIVY_REPORT": str(report_path),
        "FAKE_REPO_ROOT": str(tmp_path),
        "REAL_JQ": _tool_binary("jq"),
        "REAL_PYTHON": sys.executable,
    }
    missing = subprocess.run(
        ["make", "-C", str(tmp_path), f"MISE={fake_mise}", "scan-image"],
        env={k: v for k, v in env.items() if k != "IMAGE_REF"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0
    assert "ERROR: Set IMAGE_REF=<image:tag>" in missing.stderr

    proc = subprocess.run(
        [
            "make",
            "-C",
            str(tmp_path),
            f"MISE={fake_mise}",
            "scan-image",
            "IMAGE_REF=local-scan/test:latest",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if expected_ok:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    else:
        assert proc.returncode != 0, proc.stdout + proc.stderr
    assert list((tmp_path / ".cache/trivy/reports").glob("*.json")) == []


def test_makefile_scan_local_affected_selection_and_ci_sequential_wait(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--bare", str(origin)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    shutil.copy2(ROOT / "Makefile", repo / "Makefile")
    shutil.copy2(ROOT / "trivy.yaml", repo / "trivy.yaml")
    (repo / "tools/scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "tools/scripts/images.py", repo / "tools/scripts/images.py")
    shutil.copy2(
        ROOT / "tools/scripts/scan-images.sh", repo / "tools/scripts/scan-images.sh"
    )
    for name in ("unified-api", "platform-worker", "sandbox", "frontend"):
        img_dir = repo / "images" / name
        img_dir.mkdir(parents=True)
        (img_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    for rel in (
        "apps/core/pyproject.toml",
        "apps/sandbox-exec/pyproject.toml",
        "apps/ui/package.json",
    ):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# initial\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    fake_mise = _write_fake_scan_mise(tmp_path)
    report_path = tmp_path / "clean-report.json"
    report_path.write_text(json.dumps({"Results": []}), encoding="utf-8")
    log_path = tmp_path / "scan.log"
    env = {
        **os.environ,
        "FAKE_SCAN_LOG": str(log_path),
        "FAKE_TRIVY_REPORT": str(report_path),
        "FAKE_REPO_ROOT": str(repo),
        "REAL_JQ": _tool_binary("jq"),
        "REAL_PYTHON": sys.executable,
        "CI": "true",
    }

    clean = subprocess.run(
        ["make", "-C", str(repo), f"MISE={fake_mise}", "scan-local"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "No image or runtime dependency files modified" in clean.stdout

    subprocess.run(
        ["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True
    )
    (repo / "apps/sandbox-exec/pyproject.toml").write_text(
        "# changed\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "apps/sandbox-exec/pyproject.toml"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "change sandbox pyproject"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "feature"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "apps/ui/package.json").write_text("# dirty ui\n", encoding="utf-8")

    affected = subprocess.run(
        ["make", "-C", str(repo), f"MISE={fake_mise}", "scan-local"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert affected.returncode == 0, affected.stdout + affected.stderr
    for expected_image in ("unified-api", "platform-worker", "sandbox", "frontend"):
        assert f"PASS {expected_image}" in affected.stdout

    unknown = subprocess.run(
        [
            "make",
            "-C",
            str(repo),
            f"MISE={fake_mise}",
            "scan-local",
            "IMAGE=does-not-exist",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unknown.returncode == 2
    assert "ERROR: Unknown image selection: does-not-exist" in unknown.stderr
