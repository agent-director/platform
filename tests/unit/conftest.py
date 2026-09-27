import pytest

from gateway.unified_api import app, get_admin_allowlist


@pytest.fixture
def mock_admin_allowlist():
    app.dependency_overrides[get_admin_allowlist] = lambda: {
        "admins": ["admin@example.com"]
    }
    yield
    app.dependency_overrides.clear()
