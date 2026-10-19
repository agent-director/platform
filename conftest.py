import os
from pathlib import Path

import pytest

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
    os.environ.setdefault("ROBYN_CLI", "true")
    for variable in _GIT_HOOK_VARS:
        os.environ.pop(variable, None)
    if config.option.basetemp:
        Path(config.option.basetemp).parent.mkdir(parents=True, exist_ok=True)
