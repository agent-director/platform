"""Native projection, cache, discovery and bootstrap safety contracts.

Fixtures live under pytest's project-local temporary root. No real Git hooks,
network downloads, dependency syncs or installed tool trees are mutated here.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import toolchain


@pytest.fixture
def manifest() -> dict[str, object]:
    return toolchain.load_toml(toolchain.ROOT / "mise.toml")


@pytest.fixture
def project_root(tmp_path: Path, manifest: dict[str, object]) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "frontend").mkdir()
    (root / ".python-version").write_text(toolchain.version(manifest, "python") + "\n")
    (root / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.11"\n')
    package = {
        "packageManager": f"pnpm@{toolchain.version(manifest, 'pnpm')}",
        "devEngines": {
            "runtime": {
                "name": "node",
                "version": toolchain.version(manifest, "node"),
                "onFail": "error",
            }
        },
        "engines": {"node": ">=26.11.1 <27", "pnpm": ">=12 <13"},
    }
    (root / "package.json").write_text(json.dumps(package))
    (root / "frontend/package.json").write_text(
        json.dumps({"engines": {"node": ">=26.11.1 <27"}})
    )
    (root / "pnpm-workspace.yaml").write_text(
        yaml.safe_dump(
            {
                "packages": ["frontend"],
                "storeDir": "${PLATFORM_CACHE_DIR}/pnpm/store",
                "cacheDir": "${PLATFORM_CACHE_DIR}/pnpm/cache",
                "pmOnFail": "error",
                "runtimeOnFail": "error",
                "engineStrict": True,
            }
        )
    )
    return root


@pytest.fixture
def launcher_root(tmp_path: Path) -> Path:
    """Exercise the real launcher with a checksum-pinned, local fake mise binary."""
    root = tmp_path / "launcher-project"
    (root / "scripts").mkdir(parents=True)
    (root / "frontend").mkdir()
    launcher = root / "scripts/mise.sh"
    launcher.write_text((toolchain.ROOT / "scripts/mise.sh").read_text())
    launcher.chmod(0o755)
    binary_content = (
        "#!/bin/sh\n"
        'if [ "${1-}" = exec ]; then\n'
        '  [ "${2-}" = -- ] || exit 2\n'
        "  shift 2\n"
        "  export PLATFORM_MISE_ACTIVE=1\n"
        '  export PATH="$PLATFORM_ROOT/bin:$PATH"\n'
        '  exec "$@"\n'
        "fi\n"
        'printf "%s\\n" "$PLATFORM_CACHE_DIR" "$UV_PROJECT_ENVIRONMENT" "$XDG_RUNTIME_DIR" "$PWD" "$MISE_TMP_DIR" "${MISE_NODE_VERSION-unset}" "${MISE_PYTHON_VERSION-unset}" "${MISE_IGNORED_CONFIG_PATHS-unset}" "$MISE_CONFIG_DIR" "$MISE_SYSTEM_CONFIG_DIR" "${MISE_PYTHON_UV_VENV_AUTO-unset}"\n'
    )
    checksum = hashlib.sha256(binary_content.encode()).hexdigest()
    manifest_text = (toolchain.ROOT / "mise.toml").read_text()
    manifest_text = re.sub(
        r'^(mise_sha256_\w+ = ")[a-f0-9]+("$)',
        rf"\g<1>{checksum}\g<2>",
        manifest_text,
        flags=re.MULTILINE,
    )
    (root / "mise.toml").write_text(manifest_text)
    manifest = toolchain.load_toml(root / "mise.toml")
    metadata = toolchain.table(manifest["_"], "_")
    operating_system = "macos" if platform.system() == "Darwin" else "linux"
    architecture = "arm64" if platform.machine() in {"arm64", "aarch64"} else "x64"
    binary = (
        root
        / ".cache/mise/bootstrap"
        / toolchain.text(metadata["mise_version"], "mise_version")
        / f"{operating_system}-{architecture}"
        / "mise"
    )
    binary.parent.mkdir(parents=True)
    binary.write_text(binary_content)
    binary.chmod(0o755)
    return root


def test_launcher_sets_absolute_cache_before_startup_from_frontend(
    launcher_root: Path,
) -> None:
    result = subprocess.run(
        [str(launcher_root / "scripts/mise.sh"), "version"],
        cwd=launcher_root / "frontend",
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        str(launcher_root / ".cache"),
        str(launcher_root / ".venv"),
        str(launcher_root / ".cache/xdg-runtime"),
        str(launcher_root / "frontend"),
        str(launcher_root / ".cache/mise/tmp"),
        "unset",
        "unset",
        "unset",
        str(launcher_root / ".cache/mise/config"),
        str(launcher_root / ".cache/mise/system"),
        "unset",
    ]
    assert (launcher_root / ".cache/xdg-runtime").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "name",
    [
        "config/global.toml",
        "config/system.toml",
        "config/miserc.toml",
        "config/miserc.local.toml",
        "system/miserc.toml",
    ],
)
def test_cached_global_and_system_config_rejected_before_mise(
    launcher_root: Path, name: str
) -> None:
    planted = launcher_root / ".cache/mise" / name
    planted.parent.mkdir(parents=True)
    planted.write_text('[env]\nBAD = "untrusted"\n')
    result = subprocess.run(
        [str(launcher_root / "scripts/mise.sh"), "env"],
        cwd=launcher_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "unapproved cached mise configuration" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "filename",
    [
        ".tool-versions",
        ".node-version",
        ".nvmrc",
        "uv.toml",
        ".miserc-custom",
        "frontend/.python-version",
        "frontend/mise.toml",
        "frontend/pnpm-workspace.yaml",
        ".mise/conf.d/node/mise.toml",
    ],
)
def test_launcher_rejects_root_and_child_overrides(
    launcher_root: Path, filename: str
) -> None:
    override = launcher_root / filename
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("")
    result = subprocess.run(
        [str(launcher_root / "scripts/mise.sh"), "env"],
        cwd=launcher_root / "frontend",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "unapproved toolchain override" in result.stderr
    assert result.stdout == ""


def test_inherited_runtime_selectors_and_ignore_controls_are_cleared(
    launcher_root: Path,
) -> None:
    env = dict(os.environ)
    env.update(
        {
            "MISE_NODE_VERSION": "20.0.0",
            "MISE_PYTHON_VERSION": "3.13.0",
            "MISE_UV_VERSION": "0.1.0",
            "MISE_PNPM_VERSION": "11.0.0",
            "MISE_HELM_VERSION": "3.0.0",
            "MISE_IGNORED_CONFIG_PATHS": str(launcher_root / "mise.toml"),
            "MISE_TMP_DIR": str(launcher_root / "foreign-temp"),
            "MISE_CONFIG_DIR": str(launcher_root / "foreign-config"),
            "MISE_SYSTEM_CONFIG_DIR": str(launcher_root / "foreign-system"),
        }
    )
    result = subprocess.run(
        [str(launcher_root / "scripts/mise.sh"), "env"],
        cwd=launcher_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    values = result.stdout.splitlines()
    assert values[4] == str(launcher_root / ".cache/mise/tmp")
    assert values[5:8] == ["unset", "unset", "unset"]
    assert values[8:] == [
        str(launcher_root / ".cache/mise/config"),
        str(launcher_root / ".cache/mise/system"),
        "unset",
    ]


@pytest.mark.parametrize(
    "variable",
    [
        "MISE_DATA_DIR",
        "MISE_INSTALLS_DIR",
        "MISE_INSTALL_STORE_DIR",
        "MISE_SHARED_INSTALL_DIRS",
    ],
)
@pytest.mark.parametrize("cache_name", [".cache", ".CACHE", "Caches", "caches"])
def test_installed_storage_cannot_use_project_cache(
    launcher_root: Path, variable: str, cache_name: str
) -> None:
    env = dict(os.environ)
    env[variable] = str(launcher_root / cache_name / "installs")
    result = subprocess.run(
        [str(launcher_root / "scripts/mise.sh"), "env"],
        cwd=launcher_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "installed tools in disposable cache" in result.stderr
    assert result.stdout == ""


def test_installed_storage_aliases_are_rejected_without_resolving_paths(
    launcher_root: Path, tmp_path: Path
) -> None:
    destination = launcher_root / ".cache/installs"
    destination.mkdir()
    alias = tmp_path / "installed-storage"
    alias.symlink_to(destination, target_is_directory=True)
    env = dict(os.environ)
    env["MISE_INSTALL_STORE_DIR"] = str(alias)
    result = subprocess.run(
        [str(launcher_root / "scripts/mise.sh"), "env"],
        cwd=launcher_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert alias.is_symlink()


def test_existing_persistent_storage_alias_keeps_its_path_spelling(
    launcher_root: Path,
) -> None:
    # Read a standard mise runtime alias; never create fixtures outside .cache or
    # mutate installed state. Supported unit entrypoints already select mise Python.
    installation = Path(os.environ.get("UV_PYTHON", sys.base_prefix))
    alias = installation.parent / "latest"
    assert alias.is_symlink(), (
        "run toolchain tests through the supported root mise entrypoint"
    )
    before = alias.readlink()
    env = dict(os.environ)
    env["MISE_INSTALL_STORE_DIR"] = str(alias)
    subprocess.run(
        [str(launcher_root / "scripts/mise.sh"), "env"],
        cwd=launcher_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert alias.readlink() == before


@pytest.mark.parametrize("override", ["false", "create|source"])
def test_inherited_uv_venv_creation_policy_is_neutralized(
    launcher_root: Path, override: str
) -> None:
    env = dict(os.environ)
    env["MISE_PYTHON_UV_VENV_AUTO"] = override
    # The fake binary confirms that the env-setting override cannot reach mise;
    # the real manifest remains the sole source of the required "source" policy.
    env["MISE_NODE_VERSION"] = "20.0.0"
    result = subprocess.run(
        [str(launcher_root / "scripts/mise.sh"), "env"],
        cwd=launcher_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines()[-1] == "unset"
    assert not (launcher_root / ".venv").exists()


def test_launcher_rejects_corrupt_restored_bootstrap(launcher_root: Path) -> None:
    binary = next((launcher_root / ".cache/mise/bootstrap").glob("*/*/mise"))
    binary.write_text("#!/bin/sh\nexit 0\n")
    result = subprocess.run(
        [str(launcher_root / "scripts/mise.sh"), "env"],
        cwd=launcher_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "cached mise checksum mismatch" in result.stderr


@pytest.mark.parametrize(
    "outcome", ["success", "download-failure", "checksum-mismatch"]
)
def test_first_bootstrap_download_is_verified_and_cleaned(
    launcher_root: Path, tmp_path: Path, outcome: str
) -> None:
    binary = next((launcher_root / ".cache/mise/bootstrap").glob("*/*/mise"))
    payload = tmp_path / "payload"
    payload.write_text(
        binary.read_text() if outcome != "checksum-mismatch" else "#!/bin/sh\nexit 0\n"
    )
    binary.unlink()
    commands = tmp_path / "commands"
    commands.mkdir()
    curl = commands / "curl"
    if outcome == "download-failure":
        curl.write_text("#!/bin/sh\nexit 22\n")
    else:
        curl.write_text(
            '#!/bin/sh\noutput=""\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  if [ "$1" = "--output" ]; then shift; output="$1"; fi\n'
            "  shift\ndone\n"
            '[ -n "$output" ] || exit 2\n'
            'cp "$BOOTSTRAP_TEST_PAYLOAD" "$output"\n'
        )
    curl.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(commands) + os.pathsep + env["PATH"]
    env["BOOTSTRAP_TEST_PAYLOAD"] = str(payload)
    result = subprocess.run(
        [str(launcher_root / "scripts/mise.sh"), "version"],
        cwd=launcher_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if outcome == "success":
        assert result.returncode == 0
        assert binary.read_text() == payload.read_text()
        assert binary.stat().st_mode & 0o111
    else:
        assert result.returncode != 0
        assert not binary.exists()
        if outcome == "checksum-mismatch":
            assert "downloaded mise checksum mismatch" in result.stderr
    assert list((launcher_root / ".cache/mise/bootstrap").glob("download.*")) == []


@pytest.mark.parametrize(
    ("script_name", "selection"),
    [("local-container-scan.sh", "test-component"), ("run-all-local-scans.sh", "all")],
)
@pytest.mark.parametrize("inherited_root", ["foreign", "missing"])
def test_scan_entrypoints_reenter_current_launcher_from_foreign_activation(
    launcher_root: Path,
    tmp_path: Path,
    script_name: str,
    selection: str,
    inherited_root: str,
) -> None:
    script = launcher_root / "scripts" / script_name
    script.write_text((toolchain.ROOT / "scripts" / script_name).read_text())
    script.chmod(0o755)
    selected = launcher_root / "bin"
    selected.mkdir()
    calls = launcher_root / "inventory-call"
    environment = launcher_root / "inventory-env"
    uv = selected / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\0" "$@" > "$INVENTORY_CALL"\n'
        'printf "%s\\n" "$PLATFORM_ROOT" "$PLATFORM_CACHE_DIR" "$UV_PROJECT_ENVIRONMENT" "$TRIVY_CACHE_DIR" "$PWD" "${MISE_TRIVY_VERSION-unset}" > "$INVENTORY_ENV"\n'
        "exit 73\n"
    )
    uv.chmod(0o755)
    foreign = tmp_path / "foreign"
    (foreign / "bin").mkdir(parents=True)
    foreign_uv = foreign / "bin/uv"
    foreign_uv.write_text('#!/bin/sh\ntouch "$FOREIGN_EXECUTED"\nexit 91\n')
    foreign_uv.chmod(0o755)
    sentinel = foreign / "executed"
    env = {
        **os.environ,
        "PATH": str(foreign / "bin") + os.pathsep + os.environ["PATH"],
        "PLATFORM_MISE_ACTIVE": "1",
        "PLATFORM_ROOT": str(foreign) if inherited_root == "foreign" else "",
        "PLATFORM_CACHE_DIR": str(foreign / ".cache"),
        "UV_PROJECT_ENVIRONMENT": str(foreign / ".venv"),
        "TRIVY_CACHE_DIR": str(foreign / ".cache/trivy"),
        "MISE_TRIVY_VERSION": "0.70.0",
        "INVENTORY_CALL": str(calls),
        "INVENTORY_ENV": str(environment),
        "FOREIGN_EXECUTED": str(sentinel),
    }
    result = subprocess.run(
        [str(script), selection],
        cwd=launcher_root / "frontend",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # A failing inventory aborts before any Docker work; it must be the current
    # checkout's selected executable, not one inherited from the foreign shell.
    assert result.returncode == 73, result.stdout + result.stderr
    assert not sentinel.exists()
    assert calls.read_bytes().split(b"\0")[:-1] == [
        b"run",
        b"--no-project",
        b"scripts/images.py",
        b"--format",
        b"tsv",
    ]
    assert environment.read_text().splitlines() == [
        str(launcher_root),
        str(launcher_root / ".cache"),
        str(launcher_root / ".venv"),
        str(launcher_root / ".cache/trivy"),
        str(launcher_root),
        "unset",
    ]


@pytest.mark.parametrize(
    ("package_type", "finding_severity", "scanner", "execution_error", "expected"),
    [
        ("library", "HIGH", "vuln", "0", [1, 0, 0]),
        ("library", "CRITICAL", "vuln", "0", [1, 0, 0]),
        ("os", "HIGH", "vuln", "0", [0, 0, 0]),
        ("os", "CRITICAL", "vuln", "0", [0, 0, 1]),
        ("", "HIGH", "secret", "0", [1, 0, 0]),
        ("", "CRITICAL", "secret", "0", [1, 0, 1]),
        ("os", "HIGH", "vuln", "9", [9, 9, 9]),
    ],
)
def test_ci_image_gates_run_independently_with_root_selected_policy(
    launcher_root: Path,
    package_type: str,
    finding_severity: str,
    scanner: str,
    execution_error: str,
    expected: list[int],
) -> None:
    workflow = yaml.safe_load(
        (toolchain.ROOT / ".github/workflows/_build-scan-push.yml").read_text()
    )
    steps = workflow["jobs"]["build-scan-push"]["steps"]
    names = [
        "Enforce application vulnerability policy",
        "Report high-severity operating-system vulnerabilities",
        "Enforce critical operating-system vulnerability policy",
    ]
    gates = [step for step in steps if step.get("name") in names]
    assert [step["name"] for step in gates] == names
    push = next(step for step in steps if step.get("name") == "Push scanned image")
    assert steps.index(push) > max(steps.index(gate) for gate in gates)
    assert (
        push["if"] == "inputs.push_image"
    )  # GitHub's default success() still gates push.
    selected = launcher_root / "bin"
    selected.mkdir()
    trivy = selected / "trivy"
    trivy.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'printf "%s\\0" "$@" > "$TRIVY_ARGUMENTS"\n'
        'printf "%s\\n" "$PLATFORM_ROOT" "$TRIVY_CACHE_DIR" "${MISE_TRIVY_VERSION-unset}" > "$TRIVY_ENVIRONMENT"\n'
        '[ "$TRIVY_EXECUTION_ERROR" = 0 ] || exit "$TRIVY_EXECUTION_ERROR"\n'
        'packages="" severities="" scanners="" result=0\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        "    --pkg-types) packages=$2; shift ;;\n"
        "    --severity) severities=$2; shift ;;\n"
        "    --scanners) scanners=$2; shift ;;\n"
        "    --exit-code) result=$2; shift ;;\n"
        "  esac\n"
        "  shift\n"
        "done\n"
        'case ",$scanners," in *",$TRIVY_FINDING_SCANNER,"*) ;; *) exit 0 ;; esac\n'
        'case ",$severities," in *",$TRIVY_FINDING_SEVERITY,"*) ;; *) exit 0 ;; esac\n'
        'if [ "$TRIVY_FINDING_SCANNER" = vuln ] && [ "$packages" != "$TRIVY_FINDING_PACKAGE" ]; then exit 0; fi\n'
        'exit "$result"\n'
    )
    trivy.chmod(0o755)
    # Deliberately plant the incompatible IaC policy. Image invocation must
    # disable config loading, not inherit this file's scanners and skip settings.
    (launcher_root / "trivy.yaml").write_text(
        "scan:\n  scanners: [misconfig, secret]\ndb:\n  skip-update: true\nexit-code: 0\n"
    )
    for index, (gate, packages, severities, exit_code) in enumerate(
        zip(
            gates,
            ["library", "os", "os"],
            ["HIGH,CRITICAL", "HIGH", "CRITICAL"],
            ["1", "0", "1"],
            strict=True,
        )
    ):
        assert "uses" not in gate
        assert not gate.get("continue-on-error")
        assert "if" not in gate
        assert gate["env"]["IMAGE_REF"] == "${{ inputs.image }}:${{ github.sha }}"
        arguments = launcher_root / f"trivy-{index}.argv"
        environment = launcher_root / f"trivy-{index}.env"
        env = {
            **os.environ,
            "IMAGE_REF": "ghcr.io/example/runtime:test",
            "PLATFORM_MISE_ACTIVE": "1",
            "PLATFORM_ROOT": str(launcher_root / "foreign"),
            "TRIVY_CACHE_DIR": str(launcher_root / "foreign-cache"),
            "MISE_TRIVY_VERSION": "0.70.0",
            "TRIVY_ARGUMENTS": str(arguments),
            "TRIVY_ENVIRONMENT": str(environment),
            "TRIVY_FINDING_PACKAGE": package_type,
            "TRIVY_FINDING_SEVERITY": finding_severity,
            "TRIVY_FINDING_SCANNER": scanner,
            "TRIVY_EXECUTION_ERROR": execution_error,
        }
        # Run each CI step in a fresh shell, not inside the local Docker scan
        # wrapper or an environment prepared by a preceding action invocation.
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", gate["run"]],
            cwd=launcher_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == expected[index], result.stdout + result.stderr
        actual = [
            argument.decode() for argument in arguments.read_bytes().split(b"\0")[:-1]
        ]
        assert actual[:1] == ["image"]
        assert actual[-1] == env["IMAGE_REF"]
        for flag, value in {
            "--config": "",
            "--scanners": "vuln,secret",
            "--format": "table",
            "--pkg-types": packages,
            "--severity": severities,
            "--exit-code": exit_code,
        }.items():
            assert actual[actual.index(flag) + 1] == value
        assert "--skip-db-update=false" in actual
        assert "--skip-java-db-update=false" in actual
        assert "--ignore-unfixed" not in actual
        assert environment.read_text().splitlines() == [
            str(launcher_root),
            str(launcher_root / ".cache/trivy"),
            "unset",
        ]


@pytest.mark.parametrize(
    ("ci", "arguments", "install_hooks"),
    [("true", [], False), ("false", ["--no-hooks"], False), ("false", [], True)],
)
def test_setup_ignores_foreign_worktree_activation(
    tmp_path: Path, ci: str, arguments: list[str], install_hooks: bool
) -> None:
    root = tmp_path / "setup-project"
    (root / "scripts").mkdir(parents=True)
    setup = root / "scripts/dev-setup.sh"
    setup.write_text((toolchain.ROOT / "scripts/dev-setup.sh").read_text())
    setup.chmod(0o755)
    calls = root / "calls"
    launcher = root / "scripts/mise.sh"
    launcher.write_text(f'#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$*" >> "{calls}"\n')
    launcher.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "PLATFORM_MISE_ACTIVE": "1",
            "PLATFORM_ROOT": str(tmp_path / "foreign"),
            "UV_PROJECT_ENVIRONMENT": str(tmp_path / "foreign/.venv"),
            "CI": ci,
        }
    )
    result = subprocess.run(
        [str(setup), *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    recorded = calls.read_text().splitlines()
    assert len(recorded) == (6 if install_hooks else 5)
    assert all(line.startswith(str(root) + "|") for line in recorded)
    assert any("exec -- uv sync --locked" in line for line in recorded)
    assert any("exec -- pnpm install --frozen-lockfile" in line for line in recorded)
    assert (
        any(
            "exec -- uv run --locked scripts/toolchain.py install-hooks" in line
            for line in recorded
        )
        is install_hooks
    )
    assert "make up" not in result.stdout
    makefile = (toolchain.ROOT / "Makefile").read_text()
    for target, cluster_name in [
        ("cluster-up", "unique-name"),
        ("test-integration", "same-name"),
        ("teardown", "same-name"),
    ]:
        assert f"make {target} CLUSTER_NAME=<{cluster_name}>" in result.stdout
        assert re.search(rf"^{re.escape(target)}:", makefile, flags=re.MULTILINE)


def test_query_shebang_does_not_sync_application_environment() -> None:
    first_line = (toolchain.ROOT / "scripts/toolchain.py").read_text().splitlines()[0]
    assert first_line == "#!/usr/bin/env -S uv run --no-project"


def test_manifest_preserves_uv_environment_ownership(
    manifest: dict[str, object],
) -> None:
    assert toolchain.check_manifest(manifest) == []
    env = toolchain.table(manifest["env"], "env")
    assert env["UV_PROJECT_ENVIRONMENT"] == "{{ config_root }}/.venv"
    assert env["UV_PYTHON"] == {"value": "{{ tools.python.path }}", "tools": True}
    assert "MISE_DATA_DIR" not in env
    assert "MISE_INSTALLS_DIR" not in env


@pytest.mark.parametrize("bad_pin", ["latest", "3.14", ">=3.14", "3.14.8rc1"])
def test_tool_selection_requires_exact_releases(
    manifest: dict[str, object], bad_pin: str
) -> None:
    toolchain.table(manifest["tools"], "tools")["python"] = bad_pin
    with pytest.raises(toolchain.ToolchainError, match="exact release"):
        toolchain.version(manifest, "python")


def test_python_projection_is_exact_and_deterministic(
    project_root: Path, manifest: dict[str, object]
) -> None:
    (project_root / ".python-version").write_text("3.14\n")
    assert any(
        ".python-version" in error
        for error in toolchain.check_native_metadata(project_root, manifest)
    )
    original_package = (project_root / "package.json").read_text()
    toolchain.project(project_root, manifest)
    assert (project_root / ".python-version").read_text() == toolchain.version(
        manifest, "python"
    ) + "\n"
    assert (project_root / "package.json").read_text() == original_package
    assert toolchain.check_native_metadata(project_root, manifest) == []


@pytest.mark.parametrize(
    "filename",
    [
        "frontend/mise.toml",
        "frontend/.mise.local.toml",
        "frontend/.mise/config.toml",
        "frontend/mise/conf.d/tools.toml",
        "frontend/.config/mise/config.local.toml",
        "frontend/.miserc.toml",
        "frontend/.python-version",
        "frontend/.node-version",
        "frontend/.nvmrc",
        "frontend/.tool-versions",
        "frontend/uv.toml",
        "frontend/pnpm-workspace.yaml",
        "mise.local.toml",
    ],
)
def test_child_and_alternate_tool_overrides_fail(
    project_root: Path, manifest: dict[str, object], filename: str
) -> None:
    override = project_root / filename
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("")
    errors = toolchain.check_native_metadata(project_root, manifest)
    assert any(filename in error for error in errors)


@pytest.mark.parametrize("directory", [".cache", ".venv", "node_modules"])
def test_generated_dependency_metadata_is_not_a_child_override(
    project_root: Path, manifest: dict[str, object], directory: str
) -> None:
    excluded = project_root / directory
    excluded.mkdir()
    (excluded / "mise.toml").write_text('[tools]\nnode = "1.0.0"\n')
    (excluded / "package.json").write_text('{"engines":{"node":"<1"}}')
    assert toolchain.check_native_metadata(project_root, manifest) == []


@pytest.mark.parametrize("runtime", ["node", "pnpm"])
def test_native_manager_and_runtime_projections_detect_drift(
    project_root: Path, manifest: dict[str, object], runtime: str
) -> None:
    package = toolchain.load_json(project_root / "package.json")
    if runtime == "pnpm":
        package["packageManager"] = "pnpm@12.0.0"
    else:
        toolchain.table(
            toolchain.table(package["devEngines"], "devEngines")["runtime"], "runtime"
        )["version"] = "26.10.0"
    (project_root / "package.json").write_text(json.dumps(package))
    assert any(
        "differs" in error
        for error in toolchain.check_native_metadata(project_root, manifest)
    )


@pytest.mark.parametrize(
    "selection",
    [
        {"packageManager": "pnpm@12.11.2"},
        {"devEngines": {"runtime": {"name": "node", "version": "26.11.1"}}},
    ],
)
def test_frontend_cannot_add_an_independent_tool_selection(
    project_root: Path, manifest: dict[str, object], selection: dict[str, object]
) -> None:
    (project_root / "frontend/package.json").write_text(json.dumps(selection))
    assert any(
        "independent" in error
        for error in toolchain.check_native_metadata(project_root, manifest)
    )


def test_compatibility_ranges_remain_ranges(
    project_root: Path, manifest: dict[str, object]
) -> None:
    before_python = (project_root / "pyproject.toml").read_text()
    before_package = (project_root / "package.json").read_text()
    assert toolchain.check_native_metadata(project_root, manifest) == []
    toolchain.project(project_root, manifest)
    assert (project_root / "pyproject.toml").read_text() == before_python
    assert (project_root / "package.json").read_text() == before_package


@pytest.mark.parametrize("constraint", ["<3.14", ">=3.15", "not-a-python-range"])
def test_python_compatibility_mismatch_fails(
    project_root: Path, manifest: dict[str, object], constraint: str
) -> None:
    (project_root / "pyproject.toml").write_text(
        f'[project]\nrequires-python = "{constraint}"\n'
    )
    assert any(
        "requires-python" in error
        for error in toolchain.check_native_metadata(project_root, manifest)
    )


@pytest.mark.parametrize(
    "constraint", [">=27", "^25.1.0", ">=26.12 <27", "not-a-node-range"]
)
def test_node_compatibility_mismatch_fails(
    project_root: Path, manifest: dict[str, object], constraint: str
) -> None:
    (project_root / "frontend/package.json").write_text(
        json.dumps({"engines": {"node": constraint}})
    )
    assert any(
        "node" in error
        for error in toolchain.check_native_metadata(project_root, manifest)
    )


def test_disposable_cache_paths_must_not_be_relative(
    manifest: dict[str, object],
) -> None:
    toolchain.table(manifest["env"], "env")["PREK_HOME"] = ".cache/prek"
    assert any("PREK_HOME" in error for error in toolchain.check_manifest(manifest))


@pytest.mark.parametrize("location", ["settings", "tool_config"])
@pytest.mark.parametrize("value", [False, None])
def test_native_locking_policy_cannot_be_disabled(
    manifest: dict[str, object], location: str, value: bool | None
) -> None:
    key = "lockfile" if location == "settings" else "locked"
    section = toolchain.table(manifest[location], location)
    if value is None:
        section.pop(key)
    else:
        section[key] = value
    assert any(
        "must remain true" in error for error in toolchain.check_manifest(manifest)
    )


def test_installed_interpreters_cannot_be_moved_under_cache(
    manifest: dict[str, object],
) -> None:
    toolchain.table(manifest["env"], "env")["MISE_INSTALLS_DIR"] = (
        "{{ config_root }}/.cache/installs"
    )
    assert any(
        "installed interpreter" in error for error in toolchain.check_manifest(manifest)
    )


def test_kubectl_must_match_kind_kubernetes_skew(manifest: dict[str, object]) -> None:
    toolchain.table(manifest["tools"], "tools")["kubectl"] = "1.34.1"
    assert any(
        "one-minor skew" in error for error in toolchain.check_manifest(manifest)
    )


def test_build_inputs_are_derived_from_root_pins(manifest: dict[str, object]) -> None:
    inputs = toolchain.build_args(manifest)
    assert inputs["PYTHON_VERSION"] == toolchain.version(manifest, "python")
    assert inputs["UV_VERSION"] == toolchain.version(manifest, "uv")
    assert inputs["UV_IMAGE"] == toolchain.image(manifest, "uv")
    assert inputs["NODE_RUNTIME_IMAGE"] == toolchain.image(manifest, "node_runtime")
    toolchain.table(manifest["tools"], "tools")["uv"] = "0.13.0"
    assert toolchain.build_args(manifest)["UV_IMAGE"].startswith(
        "ghcr.io/astral-sh/uv:0.13.0-trixie-slim@sha256:"
    )


def test_static_native_image_projections_detect_drift(
    project_root: Path, manifest: dict[str, object]
) -> None:
    directory = project_root / "images/example"
    directory.mkdir(parents=True)
    dockerfile = directory / "Dockerfile"
    dockerfile.write_text("FROM " + toolchain.image(manifest, "uv") + "\n")
    assert toolchain.check_build_inputs(project_root, manifest) == []
    dockerfile.write_text(
        "FROM ghcr.io/astral-sh/uv:0.1.0-trixie-slim@sha256:" + "a" * 64 + "\n"
    )
    assert any(
        "static uv" in error
        for error in toolchain.check_build_inputs(project_root, manifest)
    )


def test_historical_image_comments_do_not_create_drift(
    project_root: Path, manifest: dict[str, object]
) -> None:
    directory = project_root / "images/example"
    directory.mkdir(parents=True)
    (directory / "Dockerfile").write_text(
        "# Historical builder node:20\nFROM ${NODE_IMAGE}\n"
    )
    assert toolchain.check_build_inputs(project_root, manifest) == []


@pytest.mark.parametrize(
    "reference",
    [
        "gcr.io/distroless/nodejs25-debian13:nonroot@sha256:" + "a" * 64,
        "gcr.io/distroless/nodejs26-debian13:nonroot@sha256:" + "b" * 64,
    ],
)
def test_distroless_base_projection_detects_major_and_digest_drift(
    project_root: Path, manifest: dict[str, object], reference: str
) -> None:
    directory = project_root / "images/example"
    directory.mkdir(parents=True)
    dockerfile = directory / "Dockerfile"
    dockerfile.write_text("FROM " + toolchain.image(manifest, "node_runtime") + "\n")
    assert toolchain.check_build_inputs(project_root, manifest) == []
    dockerfile.write_text("FROM " + reference + "\n")
    assert any(
        "static node_runtime" in error
        for error in toolchain.check_build_inputs(project_root, manifest)
    )


@pytest.mark.parametrize(
    "reference",
    [
        "node",
        "node@sha256:" + "a" * 64,
        "ghcr.io/astral-sh/uv@sha256:" + "b" * 64,
        "ghcr.io/pnpm/pnpm@sha256:" + "c" * 64,
        "gcr.io/distroless/nodejs26-debian13@sha256:" + "d" * 64,
    ],
)
@pytest.mark.parametrize(
    "instruction", ["FROM {reference}", "COPY --from={reference} /source /destination"]
)
def test_bare_and_digest_only_images_cannot_bypass_root_projection(
    project_root: Path, manifest: dict[str, object], reference: str, instruction: str
) -> None:
    directory = project_root / "images/example"
    directory.mkdir(parents=True)
    (directory / "Dockerfile").write_text(
        instruction.format(reference=reference) + "\n"
    )
    assert any(
        "static" in error
        for error in toolchain.check_build_inputs(project_root, manifest)
    )


def test_stage_alias_cannot_hide_its_own_unpinned_base(
    project_root: Path, manifest: dict[str, object]
) -> None:
    directory = project_root / "images/example"
    directory.mkdir(parents=True)
    (directory / "Dockerfile").write_text(
        "FROM node AS node\nCOPY --from=node /source /destination\n"
    )
    assert any(
        "static node" in error
        for error in toolchain.check_build_inputs(project_root, manifest)
    )


def test_named_stage_is_not_an_external_image(
    project_root: Path, manifest: dict[str, object]
) -> None:
    directory = project_root / "images/example"
    directory.mkdir(parents=True)
    (directory / "Dockerfile").write_text(
        "FROM ${NODE_IMAGE} AS node\nFROM node AS runner\nCOPY --from=node /source /destination\n"
    )
    assert toolchain.check_build_inputs(project_root, manifest) == []


@pytest.mark.parametrize(
    "instruction", ["COPY <<EOF /config", "RUN <<'EOF'", "COPY <<-EOF /config"]
)
def test_heredoc_payloads_are_not_dockerfile_inputs(
    project_root: Path, manifest: dict[str, object], instruction: str
) -> None:
    directory = project_root / "images/example"
    directory.mkdir(parents=True)
    prefix = "\t" if "<<-" in instruction else ""
    (directory / "Dockerfile").write_text(
        "FROM ${NODE_IMAGE}\n"
        + instruction
        + "\nFROM node:20\nARG PYTHON_VERSION=1.0.0\n"
        + prefix
        + "EOF\n"
    )
    assert toolchain.check_build_inputs(project_root, manifest) == []


def test_build_arg_defaults_detect_drift(
    project_root: Path, manifest: dict[str, object]
) -> None:
    directory = project_root / "images/example"
    directory.mkdir(parents=True)
    (directory / "Dockerfile").write_text(
        "ARG PYTHON_VERSION=3.13.0\nFROM ${UV_IMAGE}\n"
    )
    assert any(
        "PYTHON_VERSION" in error
        for error in toolchain.check_build_inputs(project_root, manifest)
    )


def test_live_discovery_is_exercised_from_root_and_frontend(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], Path]] = []

    def run(
        command: list[str], *, cwd: Path, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        if "config" in command:
            value: object = [{"path": str(project_root / "mise.toml")}]
        else:
            env = {
                key: str(project_root / ".cache") + suffix
                for key, suffix in toolchain.CACHE_PATHS.items()
            }
            env["UV_PYTHON"] = (
                "/home/user/.local/share/mise/installs/python/3.14.8/bin/python"
            )
            env["UV_PROJECT_ENVIRONMENT"] = str(project_root / ".venv")
            value = env
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(value), stderr=""
        )

    monkeypatch.setattr(toolchain.subprocess, "run", run)
    assert toolchain.check_environment(project_root) == []
    assert [cwd for _, cwd in calls] == [
        project_root,
        project_root,
        project_root / "frontend",
        project_root / "frontend",
    ]


@pytest.mark.parametrize("wrong_node", [False, True])
def test_effective_runtime_versions_are_probed_from_both_directories(
    project_root: Path,
    manifest: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    wrong_node: bool,
) -> None:
    calls: list[tuple[str, Path]] = []

    def run(
        command: list[str], *, cwd: Path, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        tool = "python" if "python" in command[3:] else command[3]
        calls.append((tool, cwd))
        selected = (
            "20.0.0"
            if tool == "node" and wrong_node
            else toolchain.version(manifest, tool)
        )
        return subprocess.CompletedProcess(
            command, 0, stdout=f"v{selected}\n", stderr=""
        )

    monkeypatch.setattr(toolchain.subprocess, "run", run)
    errors = toolchain.check_selected_versions(project_root, manifest)
    assert len(calls) == 24
    assert {cwd for _, cwd in calls} == {project_root, project_root / "frontend"}
    assert bool(errors) is wrong_node
    if wrong_node:
        assert len(errors) == 2
        assert all("node executable differs" in error for error in errors)


def test_shim_prefix_preserves_stock_native_and_legacy_behavior(tmp_path: Path) -> None:
    hook = tmp_path / "pre-commit"
    stock = '#!/bin/sh\n# File generated by prek: https://github.com/j178/prek\n# ID: 182c10f181da4464a3eec51b83331688\nexec "$PREK" hook-impl --hook-dir "$HERE" --script-version 4 -- "$@"\n'
    hook.write_text(stock)
    hook.chmod(0o755)
    toolchain.patch_hook(hook)
    first = hook.read_text()
    assert first.startswith("#!/bin/sh\n" + toolchain.HOOK_BEGIN)
    assert stock.split("\n", 1)[1] in first
    assert hook.stat().st_mode & 0o111
    assert "--force" not in first
    assert "core.hooksPath" not in first
    toolchain.patch_hook(hook)
    assert hook.read_text() == first
    subprocess.run(["sh", "-n", str(hook)], check=True)


def test_shim_replacement_is_atomic_and_preserves_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = tmp_path / "pre-push"
    original = (
        "#!/bin/sh\n# File generated by prek: https://github.com/j178/prek\nexit 0\n"
    )
    hook.write_text(original)
    hook.chmod(0o750)
    original_replace = os.replace
    replaced: list[Path] = []

    def replace(source: Path, destination: Path) -> None:
        assert hook.read_text() == original
        assert source.parent == hook.parent
        assert source.read_text().startswith("#!/bin/sh\n" + toolchain.HOOK_BEGIN)
        assert source.stat().st_mode & 0o777 == 0o750
        original_replace(source, destination)
        replaced.append(destination)

    monkeypatch.setattr(toolchain.os, "replace", replace)
    toolchain.patch_hook(hook)
    assert replaced == [hook]
    assert hook.stat().st_mode & 0o777 == 0o750
    assert list(tmp_path.iterdir()) == [hook]


def test_unrecognized_git_hook_is_never_patched(tmp_path: Path) -> None:
    hook = tmp_path / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    before = hook.read_text()
    with pytest.raises(toolchain.ToolchainError, match="unrecognized"):
        toolchain.patch_hook(hook)
    assert hook.read_text() == before


@pytest.mark.parametrize("failure", ["bad-root", "missing-launcher", "failed-mise-env"])
def test_gui_shim_fails_closed_without_a_trusted_root(
    tmp_path: Path, failure: str
) -> None:
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    git = bin_directory / "git"
    git.write_text(
        "#!/bin/sh\nexit 1\n"
        if failure == "bad-root"
        else f"#!/bin/sh\nprintf '%s\\n' '{root}'\n"
    )
    git.chmod(0o755)
    if failure == "failed-mise-env":
        launcher = root / "scripts/mise.sh"
        launcher.write_text("#!/bin/sh\nexit 1\n")
        launcher.chmod(0o755)
    sentinel = tmp_path / "unexpected"
    hook = tmp_path / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n" + toolchain.hook_environment_prefix() + f"touch '{sentinel}'\n"
    )
    hook.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bin_directory) + os.pathsep + env["PATH"]
    result = subprocess.run(
        [str(hook)], env=env, check=False, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert not sentinel.exists()


def test_gui_shim_uses_root_cache_without_shell_activation(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    git = bin_directory / "git"
    git.write_text(f"#!/bin/sh\nprintf '%s\\n' '{root}'\n")
    git.chmod(0o755)
    launcher = root / "scripts/mise.sh"
    launcher.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"export PREK_HOME='{root}/.cache/prek'\"\n"
    )
    launcher.chmod(0o755)
    hook = tmp_path / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        + toolchain.hook_environment_prefix()
        + "printf '%s\\n' \"$PREK_HOME\"\n"
    )
    hook.chmod(0o755)
    env = dict(os.environ)
    env.pop("PREK_HOME", None)
    env["PATH"] = str(bin_directory) + os.pathsep + env["PATH"]
    result = subprocess.run(
        [str(hook)], env=env, check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == str(root / ".cache/prek")
