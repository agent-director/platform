import pytest

from gateway.unified_api import _allowlist_cache, _config_cache, app, get_admin_allowlist


@pytest.fixture
def mock_admin_allowlist():
    app.dependency_overrides[get_admin_allowlist] = lambda: {
        "admins": ["admin@example.com"]
    }
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_global_caches():
    """Reset the global cache states before and after each unit test."""
    _allowlist_cache._data = None
    _allowlist_cache._time = 0.0
    _config_cache._data = None
    _config_cache._time = 0.0
    yield
    _allowlist_cache._data = None
    _allowlist_cache._time = 0.0
    _config_cache._data = None
    _config_cache._time = 0.0
