import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).absolute().parents[1]


@pytest.fixture(scope="session")
def chart_dir(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Copy charts into an isolated session directory so tests never mutate tracked files."""
    directory = tmp_path_factory.mktemp("helm-charts")
    charts_dir = directory / "charts"
    shutil.copytree(REPO_ROOT / "charts", charts_dir)
    chart_path = charts_dir / "platform"
    assert not (chart_path / "charts").exists()
    assert (chart_path / "templates/temporal.yaml").is_file()
    return str(chart_path)
