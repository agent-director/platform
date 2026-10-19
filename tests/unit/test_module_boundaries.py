"""AST-level module boundary verification across uv workspace packages."""

import ast
from pathlib import Path

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
    files = (
        _python_files_under("apps/core")
        + _python_files_under("tools/scripts")
        + _python_files_under("scripts")
    )
    assert files, "Expected Python files in apps/core and scripts"
    for py_file in files:
        overlap = _imported_top_level_modules(py_file) & SANDBOX_MODULES
        assert not overlap, (
            f"{py_file.relative_to(REPO_ROOT)} imports forbidden {overlap}"
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
