import shutil
from pathlib import Path

import pytest

from scripts.helm_dependencies import validate_materialized

REPO_ROOT = Path(__file__).absolute().parents[1]


def pytest_configure(config: pytest.Config) -> None:
    if config.option.basetemp:
        Path(config.option.basetemp).parent.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session")
def chart_dir(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Build locked dependencies in a copy, never mutate a tracked chart/lock."""
    directory = tmp_path_factory.mktemp("helm-charts")
    charts_dir = directory / "charts"
    shutil.copytree(REPO_ROOT / "charts", charts_dir)
    chart_path = charts_dir / "platform"
    # Setup materializes dependencies once; unit tests validate copied artifacts
    # offline rather than downloading repositories for every fixture session.
    for metadata in sorted(charts_dir.glob("*/Chart.yaml")):
        validate_materialized(metadata.parent)
    return str(chart_path)
