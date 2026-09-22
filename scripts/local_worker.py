import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

mcp = MCPServer("LocalFilesystem")
WORKSPACE_BOUNDARY = Path("/workspace/agent-mount").resolve()


def _secure_resolve(path: str) -> Path:
    """
    Resolves the given path strictly within the WORKSPACE_BOUNDARY.
    Rejects any traversal attempts or symlink escapes.
    """
    try:
        # Convert to Path and resolve it absolutely (resolves symlinks and ../)
        requested_path = (WORKSPACE_BOUNDARY / path).resolve()

        # Check if the resolved path is still inside the boundary
        # is_relative_to is Python 3.9+
        if not requested_path.is_relative_to(WORKSPACE_BOUNDARY):
            raise ValueError(f"Path traversal detected: {path}")

        return requested_path
    except Exception as e:
        raise ValueError(f"Invalid path: {e!s}")


@mcp.tool()
@activity.defn
async def read_file(path: str) -> str:
    """Reads a file securely within the workspace boundary."""
    secure_path = await asyncio.to_thread(_secure_resolve, path)

    def _do_read():
        if secure_path.exists():
            if not secure_path.is_file():
                raise IsADirectoryError(f"Is not a regular file: {path}")
            size = secure_path.stat().st_size
            if size > 10 * 1024 * 1024:
                raise ValueError(f"File too large: {path} exceeds 10MB limit")

        try:
            with open(secure_path, "r") as f:
                return f.read()
        except IsADirectoryError:
            raise IsADirectoryError(f"Is a directory: {path}")
        except FileNotFoundError:
            raise FileNotFoundError(f"File not found: {path}")

    return await asyncio.to_thread(_do_read)


@mcp.tool()
@activity.defn
async def write_file(path: str, content: str) -> str:
    """Writes a file securely within the workspace boundary."""
    secure_path = await asyncio.to_thread(_secure_resolve, path)

    def _do_write():
        if secure_path.is_dir():
            raise IsADirectoryError(f"Cannot overwrite directory: {path}")

        # Ensure parent directory exists
        secure_path.parent.mkdir(parents=True, exist_ok=True)

        fd, temp_path = tempfile.mkstemp(dir=secure_path.parent)
        try:
            with open(fd, "w") as f:
                f.write(content)
            os.replace(temp_path, secure_path)
        except Exception:
            os.unlink(temp_path)
            raise

    await asyncio.to_thread(_do_write)
    return f"Successfully wrote to {path}"


# Temporal setup
async def main():
    print(f"Starting Local MCP Server bounded to {WORKSPACE_BOUNDARY}", file=sys.stderr)

    # In a real scenario, this would connect to the remote Temporal cluster.
    # We use a dummy address for structure.
    temporal_address = os.environ.get("TEMPORAL_URL", "localhost:7233")
    print(f"Connecting to Temporal at {temporal_address}", file=sys.stderr)

    try:
        client = await Client.connect(temporal_address)
        worker = Worker(
            client,
            task_queue="local-mcp-queue",
            workflows=[],
            activities=[read_file, write_file],
        )
        print("Starting Temporal worker polling and MCP server...", file=sys.stderr)

        await asyncio.gather(
            worker.run(),
            mcp.run_stdio_async(),
        )
    except Exception as e:
        print(f"Startup failed: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    asyncio.run(main())
