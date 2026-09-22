import pytest
from temporalio.client import Client


@pytest.mark.asyncio
async def test_temporal_connection():
    """
    Verify connectivity against the running Temporal instance in the kind cluster without mocks.
    The CI workflow maps the Temporal NodePort to 127.0.0.1:7233.
    """
    try:
        client = await Client.connect("127.0.0.1:7233")
        assert client is not None
    except Exception as e:
        pytest.fail(f"Could not connect to Temporal at 127.0.0.1:7233: {e}")


@pytest.mark.asyncio
async def test_postgres_connection():
    """
    Verify connectivity against the running Postgres instance via NodePort mapped to localhost.
    Note: With the Zalando operator, credentials are dynamically generated. For this baseline
    test running outside the cluster, we will verify the port is open and accepting TCP
    connections rather than authenticating, since extracting the dynamic credentials
    is handled within the CI workflow or requires cluster access.
    """
    import asyncio

    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 5432)
        assert writer is not None
        writer.close()
        await writer.wait_closed()
    except Exception as e:
        pytest.fail(f"Could not connect to Postgres at 127.0.0.1:5432: {e}")
