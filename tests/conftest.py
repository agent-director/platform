import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).absolute().parents[1]
_GIT_HOOK_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_WORK_TREE",
)


def pytest_configure(config: pytest.Config) -> None:
    for variable in _GIT_HOOK_VARS:
        os.environ.pop(variable, None)
    if config.option.basetemp:
        Path(config.option.basetemp).parent.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session")
def chart_dir(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Copy charts into an isolated session directory so tests never mutate tracked files."""
    directory = tmp_path_factory.mktemp("helm-charts")
    charts_dir = directory / "charts"
    shutil.copytree(REPO_ROOT / "charts", charts_dir)
    chart_path = charts_dir / "platform"
    assert (chart_path / "charts/temporal-1.7.0.tgz").is_file(), (
        "Run `make check-helm-deps` or `make setup` to materialize chart dependencies."
    )
    return str(chart_path)
