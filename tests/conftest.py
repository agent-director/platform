import shutil
import subprocess

import pytest


@pytest.fixture(scope="session")
def chart_dir(tmp_path_factory):
    """
    Creates a temporary copy of the chart and builds dependencies.
    Prevents mutating the developer's working directory (Chart.lock, charts/).
    """
    tmp_dir = tmp_path_factory.mktemp("helm-charts")
    charts_dir = tmp_dir / "charts"
    shutil.copytree("charts", charts_dir)
    chart_path = charts_dir / "platform"
    subprocess.run(
        ["helm", "dependency", "build", str(chart_path)],
        check=True,
        capture_output=True,
    )
    return str(chart_path)
