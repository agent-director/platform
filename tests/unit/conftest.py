import pytest

from gateway.unified_api import app, get_admin_allowlist, get_config


@pytest.fixture(autouse=True)
def _reset_global_caches():
    """Reset the global cache states before and after each unit test."""
    get_admin_allowlist.clear_cache()  # type: ignore
    get_config.clear_cache()  # type: ignore
    yield
    get_admin_allowlist.clear_cache()  # type: ignore
    get_config.clear_cache()  # type: ignore


@pytest.fixture
def mock_admin_allowlist():
    app.dependency_overrides[get_admin_allowlist] = lambda: {
        "admins": ["admin@example.com"]
    }
    yield
    app.dependency_overrides.clear()
