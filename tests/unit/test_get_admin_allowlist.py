import time
from unittest.mock import mock_open, patch

import pytest
from fastapi import HTTPException

from gateway.unified_api import _allowlist_cache, get_admin_allowlist


@pytest.fixture(autouse=True)
def _reset_allowlist_cache():
    """Reset the get_admin_allowlist cache state before and after each test."""
    _allowlist_cache._cache.clear()
    yield
    _allowlist_cache._cache.clear()


@pytest.mark.asyncio
async def test_get_admin_allowlist_cache_hit_and_miss():
    yaml_content = "admins:\n  - test@example.com"
    with patch("gateway.unified_api.get_allowlist_path", return_value="dummy.yaml"):
        with patch("builtins.open", mock_open(read_data=yaml_content)) as m_open:
            # First call - cache miss
            res1 = await get_admin_allowlist()
            assert res1 == {"admins": ["test@example.com"]}
            m_open.assert_called_once_with("dummy.yaml", "r")

            # Second call - cache hit
            m_open.reset_mock()
            res2 = await get_admin_allowlist()
            assert res2 == {"admins": ["test@example.com"]}
            m_open.assert_not_called()

            # Ensure we get a copy (mutation shouldn't affect cache)
            res1["admins"].append("hacker@example.com")
            res3 = await get_admin_allowlist()
            assert res3 == {"admins": ["test@example.com"]}


@pytest.mark.asyncio
async def test_get_admin_allowlist_ttl_expiry():
    yaml_content = "admins:\n  - test@example.com"
    with patch("gateway.unified_api.get_allowlist_path", return_value="dummy.yaml"):
        with patch("builtins.open", mock_open(read_data=yaml_content)) as m_open:
            await get_admin_allowlist()
            m_open.assert_called_once()

            # Fast forward time to expire TTL
            with patch("time.time", return_value=time.time() + 301):
                m_open.reset_mock()
                await get_admin_allowlist()
                m_open.assert_called_once()


@pytest.mark.asyncio
async def test_get_admin_allowlist_file_not_found():
    with patch("gateway.unified_api.get_allowlist_path", return_value="dummy.yaml"):
        with patch("builtins.open", side_effect=FileNotFoundError):
            with pytest.raises(HTTPException) as exc:
                await get_admin_allowlist()
            assert exc.value.status_code == 500
            assert "Admin allowlist not found" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_get_admin_allowlist_malformed_yaml():
    with patch("gateway.unified_api.get_allowlist_path", return_value="dummy.yaml"):
        with patch("builtins.open", mock_open(read_data="[} invalid yaml")):
            with pytest.raises(HTTPException) as exc:
                await get_admin_allowlist()
            assert exc.value.status_code == 500
            assert "Malformed Admin allowlist" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_get_admin_allowlist_nested_data():
    yaml_content = "data:\n  allowlist.yaml: |\n    admins:\n      - nested@example.com"
    with patch("gateway.unified_api.get_allowlist_path", return_value="dummy.yaml"):
        with patch("builtins.open", mock_open(read_data=yaml_content)):
            res = await get_admin_allowlist()
            assert res == {"admins": ["nested@example.com"]}
