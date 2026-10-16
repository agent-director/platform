from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unified_api.main import app, get_admin_allowlist, get_cached_yaml


@pytest.fixture(autouse=True)
def _reset_global_caches() -> Iterator[None]:
    """Reset the global cache states before and after each unit test."""
    get_cached_yaml.cache_clear()
    yield
    get_cached_yaml.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    for name in (
        "ENV",
        "INTERNAL_SERVICE_USER",
        "INTERNAL_TOKEN",
        "CHECKPOINT_AUTH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_REPO",
        "GITHUB_CONFIG_PATH",
        "ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test:4141")
    monkeypatch.setenv("LITELLM_CONFIG_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("ADMIN_ALLOWLIST_PATH", str(tmp_path / "admins.yaml"))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def mock_admin_allowlist() -> Iterator[None]:
    app.dependency_overrides[get_admin_allowlist] = lambda: {
        "admins": ["admin@example.com"]
    }
    yield
    app.dependency_overrides.clear()
