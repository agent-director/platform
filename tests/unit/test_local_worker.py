from unittest.mock import patch

import pytest

from scripts.local_worker import (
    WORKSPACE_BOUNDARY,
    _secure_resolve,
    read_file,
    write_file,
)


def test_secure_resolve_valid_path():
    # Should resolve correctly inside boundary
    path = _secure_resolve("subdir/test.txt")
    assert path == WORKSPACE_BOUNDARY / "subdir/test.txt"


@pytest.mark.asyncio
async def test_read_and_write_file(tmp_path):
    with patch("scripts.local_worker.WORKSPACE_BOUNDARY", tmp_path):
        test_path = "test_file.txt"
        content = "Hello, Agent Control Plane!"

        # Write
        res = await write_file(test_path, content)
        assert "Successfully wrote" in res

        # Read
        read_res = await read_file(test_path)
        assert read_res == content

        # Clean up manually
        (tmp_path / test_path).unlink()


def test_secure_resolve_absolute_path_escape():
    # Attempt to use absolute path
    # If the user provides an absolute path, pathlib combines it weirdly or resolves it out
    with pytest.raises(ValueError, match="Path traversal detected"):
        _secure_resolve("/etc/passwd")


def test_secure_resolve_symlink_escape(tmp_path, monkeypatch):
    # Mock WORKSPACE_BOUNDARY to a temp dir so we can create real symlinks
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("scripts.local_worker.WORKSPACE_BOUNDARY", workspace)

    # Create a secret file outside the workspace
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    secret_file = secret_dir / "secret.txt"
    secret_file.write_text("super secret")

    # Create a symlink inside the workspace pointing to the secret file
    symlink_path = workspace / "link_to_secret"
    symlink_path.symlink_to(secret_file)

    # Now try to resolve the symlink through the secure resolver
    # Resolving the symlink will point outside the boundary, so it should be rejected.
    with pytest.raises(ValueError, match="Path traversal detected"):
        _secure_resolve("link_to_secret")
