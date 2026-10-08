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
    chart_path = tmp_dir / "platform"
    shutil.copytree("charts/platform", chart_path)
    subprocess.run(
        ["helm", "dependency", "build", str(chart_path)],
        check=True,
        capture_output=True,
    )
    return str(chart_path)
