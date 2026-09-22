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
    """
    import psycopg

    try:
        conn = await psycopg.AsyncConnection.connect(
            "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
        )
        assert conn is not None
        await conn.close()
    except Exception as e:
        pytest.fail(f"Could not connect to Postgres at 127.0.0.1:5432: {e}")
