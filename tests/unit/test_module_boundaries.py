"""AST-level module boundary, dependency isolation, and OpenAPI contract verification."""

import ast
import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from contracts import ConfigUpdateRequest, ConfigUpdateResponse, HealthResponse
from unified_api.main import create_app
from unified_api.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_MODULES = {"unified_api", "orchestrator", "director"}
SANDBOX_MODULES = {"sandbox_exec"}


def _imported_top_level_modules(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def _python_files_under(rel_dir: str) -> list[Path]:
    root = REPO_ROOT / rel_dir
    if not root.exists():
        return []
    return sorted(root.rglob("*.py"))


def _normalize_dist_name(raw_spec: str) -> str:
    base = re.split(r"[<>=!~;\[\s]", raw_spec.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", base).lower()


def _dist_to_top_level_modules() -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for top_module, dist_names in importlib.metadata.packages_distributions().items():
        for dist in dist_names:
            norm = _normalize_dist_name(dist)
            mapping.setdefault(norm, set()).add(top_module)
    mapping.setdefault("agent-director-contracts", set()).add("contracts")
    mapping.setdefault("agent-director-sandbox-exec", set()).add("sandbox_exec")
    mapping.setdefault("agent-director-core", set()).update(CONTROL_PLANE_MODULES)
    # FastAPI re-exports Starlette ASGI types as part of its core surface.
    if "fastapi" in mapping:
        mapping["fastapi"].add("starlette")
    return mapping


def test_contracts_never_imports_app_packages() -> None:
    forbidden = CONTROL_PLANE_MODULES | SANDBOX_MODULES
    files = _python_files_under("libs/contracts")
    assert files, "Expected Python files in libs/contracts"
    for py_file in files:
        overlap = _imported_top_level_modules(py_file) & forbidden
        assert not overlap, (
            f"{py_file.relative_to(REPO_ROOT)} imports forbidden {overlap}"
        )


def test_sandbox_exec_never_imports_control_plane_packages() -> None:
    files = _python_files_under("apps/sandbox-exec")
    assert files, "Expected Python files in apps/sandbox-exec"
    for py_file in files:
        overlap = _imported_top_level_modules(py_file) & CONTROL_PLANE_MODULES
        assert not overlap, (
            f"{py_file.relative_to(REPO_ROOT)} imports forbidden {overlap}"
        )


def test_core_and_scripts_never_import_sandbox_exec() -> None:
    core_files = _python_files_under("apps/core")
    script_files = _python_files_under("tools/scripts")
    assert core_files, "Expected Python files in apps/core"
    assert script_files, "Expected Python files in tools/scripts"
    assert _python_files_under("scripts") == []
    for py_file in core_files + script_files:
        overlap = _imported_top_level_modules(py_file) & SANDBOX_MODULES
        assert not overlap, (
            f"{py_file.relative_to(REPO_ROOT)} imports forbidden {overlap}"
        )


def test_package_unit_tests_never_import_integration_harness() -> None:
    for test_dir in (
        "apps/core/tests/unit",
        "apps/sandbox-exec/tests/unit",
        "libs/contracts/tests/unit",
    ):
        files = _python_files_under(test_dir)
        assert files, f"Expected unit test files under {test_dir}"
        for py_file in files:
            assert "tests" not in _imported_top_level_modules(py_file), (
                f"{py_file.relative_to(REPO_ROOT)} must not import from root tests/"
            )


def test_imported_top_level_modules_detects_synthetic_violations(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "synthetic.py"
    sample.write_text(
        "import unified_api.main as api, sandbox_exec\n"
        "from orchestrator.workflows import run\n"
        "from . import relative_helper\n",
        encoding="utf-8",
    )
    imported = _imported_top_level_modules(sample)
    assert imported == {"unified_api", "sandbox_exec", "orchestrator"}
    assert imported & CONTROL_PLANE_MODULES == {"unified_api", "orchestrator"}
    assert imported & SANDBOX_MODULES == {"sandbox_exec"}


def test_ruff_tid251_enforces_banned_imports_across_workspace_packages() -> None:
    for target_file, forbidden_import in (
        ("apps/sandbox-exec/sandbox_exec/daemon.py", "unified_api"),
        ("apps/sandbox-exec/sandbox_exec/daemon.py", "orchestrator"),
        ("apps/sandbox-exec/sandbox_exec/daemon.py", "director"),
        ("apps/core/unified_api/main.py", "sandbox_exec"),
        ("libs/contracts/contracts/schemas.py", "unified_api"),
        ("libs/contracts/contracts/schemas.py", "sandbox_exec"),
    ):
        result = subprocess.run(
            [
                "mise",
                "exec",
                "--",
                "ruff",
                "check",
                "--select",
                "TID251",
                "--stdin-filename",
                target_file,
                "-",
            ],
            input=f"import {forbidden_import}\n\nprint({forbidden_import})\n",
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0, (
            f"Expected TID251 failure for {forbidden_import} in {target_file}"
        )
        assert "TID251" in result.stdout


def test_workspace_packages_only_import_declared_dependencies() -> None:
    """Prevent the shared root .venv from masking undeclared per-package dependencies."""
    dist_map = _dist_to_top_level_modules()
    stdlib = set(sys.stdlib_module_names)

    packages = (
        ("libs/contracts", ("libs/contracts/contracts",), {"contracts"}),
        (
            "apps/sandbox-exec",
            ("apps/sandbox-exec/sandbox_exec",),
            {"sandbox_exec"},
        ),
        (
            "apps/core",
            (
                "apps/core/director",
                "apps/core/unified_api",
                "apps/core/orchestrator",
            ),
            set(CONTROL_PLANE_MODULES),
        ),
    )

    for pkg_dir, source_dirs, own_modules in packages:
        manifest = tomllib.loads((REPO_ROOT / pkg_dir / "pyproject.toml").read_text())
        declared_dists = {
            _normalize_dist_name(dep)
            for dep in manifest["project"].get("dependencies", [])
        }
        allowed_modules = set(stdlib) | set(own_modules)
        for dist in declared_dists:
            allowed_modules.update(dist_map.get(dist, set()))

        for src_dir in source_dirs:
            files = _python_files_under(src_dir)
            assert files, f"Expected Python files under {src_dir}"
            for py_file in files:
                imported = _imported_top_level_modules(py_file)
                undeclared = imported - allowed_modules
                assert not undeclared, (
                    f"{py_file.relative_to(REPO_ROOT)} imports {undeclared} not declared in {pkg_dir}/pyproject.toml"
                )


def test_nx_project_graph_enforces_layered_dependency_boundaries() -> None:
    project_files = {
        "contracts": REPO_ROOT / "libs/contracts/project.json",
        "core": REPO_ROOT / "apps/core/project.json",
        "sandbox-exec": REPO_ROOT / "apps/sandbox-exec/project.json",
        "ui": REPO_ROOT / "apps/ui/project.json",
        "charts": REPO_ROOT / "charts/project.json",
        "scripts": REPO_ROOT / "tools/scripts/project.json",
        "repo-tests": REPO_ROOT / "tests/project.json",
    }
    projects = {
        name: json.loads(path.read_text(encoding="utf-8"))
        for name, path in project_files.items()
    }

    assert projects["contracts"].get("implicitDependencies", []) == []
    for app_name in ("core", "sandbox-exec", "ui"):
        assert projects[app_name].get("implicitDependencies") == ["contracts"]
        assert set(projects[app_name]["targets"]) >= {"lint", "typecheck", "test"}

    assert set(projects["contracts"]["targets"]) >= {"lint", "typecheck", "test"}
    assert set(projects["scripts"]["targets"]) >= {"lint", "typecheck"}
    assert set(projects["repo-tests"]["targets"]) >= {"lint", "typecheck", "test"}


def test_ui_msw_handlers_match_unified_api_openapi_and_shared_contracts() -> None:
    app = create_app(Settings(litellm_url="http://localhost:4000", environment="dev"))
    openapi = app.openapi()
    paths = openapi["paths"]
    schemas = openapi["components"]["schemas"]

    handlers_src = (REPO_ROOT / "apps/ui/src/mocks/handlers.ts").read_text(
        encoding="utf-8"
    )
    mocked_routes = re.findall(
        r'http\.(get|post|put|delete|patch)\(\s*"([^"]+)"', handlers_src
    )
    assert mocked_routes == [
        ("get", "/health"),
        ("get", "/api/config"),
        ("post", "/api/config"),
        ("post", "/v1/chat/completions"),
    ]
    for method, route_path in mocked_routes:
        assert route_path in paths, f"MSW route {route_path} missing from OpenAPI"
        assert method in paths[route_path], (
            f"HTTP {method.upper()} missing on {route_path} in OpenAPI"
        )

    assert "ConfigUpdateRequest" in schemas
    assert "ConfigUpdateResponse" in schemas
    assert (
        schemas["ConfigUpdateRequest"]["properties"].keys()
        == ConfigUpdateRequest.model_json_schema()["properties"].keys()
    )
    assert (
        schemas["ConfigUpdateResponse"]["properties"].keys()
        == ConfigUpdateResponse.model_json_schema()["properties"].keys()
    )
    assert HealthResponse().status == "ok"
