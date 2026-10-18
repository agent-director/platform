from datetime import timedelta
from uuid import uuid4

import pytest
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client

from orchestrator.temporal_worker import AgentWorkflow, JobInput
from tests.integration.conftest import DatabaseEndpoint, IntegrationEnvironment


@pytest.mark.asyncio
async def test_temporal_namespace_available(
    temporal_client: Client, integration_env: IntegrationEnvironment
) -> None:
    """Make an actual SDK RPC against the selected in-cluster Temporal profile."""
    result = await temporal_client.workflow_service.describe_namespace(
        DescribeNamespaceRequest(namespace=integration_env.temporal_namespace),
        timeout=timedelta(seconds=10),
    )
    assert result.namespace_info.name == integration_env.temporal_namespace


@pytest.mark.parametrize("iteration", range(2))
def test_postgres_authenticated_round_trip(
    database_endpoint: DatabaseEndpoint, iteration: int
) -> None:
    """Authenticate as the chart's scoped platform role and perform transactional SQL."""
    with database_endpoint.connect() as connection:
        identity = connection.execute(
            "SELECT current_database(), current_user, rolsuper "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert identity == (database_endpoint.database, database_endpoint.user, False)
        assert connection.execute(
            "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
        ).fetchone() == (True,)
        connection.execute("CREATE TEMP TABLE integration_probe (value TEXT NOT NULL)")
        value = f"{iteration}-{uuid4()}"
        connection.execute(
            "INSERT INTO integration_probe (value) VALUES (%s)", (value,)
        )
        assert connection.execute("SELECT value FROM integration_probe").fetchone() == (
            value,
        )


@pytest.mark.asyncio
async def test_deployed_worker_executes_temporal_workflow(
    temporal_client: Client, integration_env: IntegrationEnvironment
) -> None:
    """Execute the existing workflow/activity on deployed workers, not the API stub."""
    workflow_id = f"integration-worker-{uuid4()}"
    result = await temporal_client.execute_workflow(
        AgentWorkflow.run,
        JobInput(job_id=workflow_id, workflow_type="ci_pipeline"),
        id=workflow_id,
        task_queue=integration_env.temporal_task_queue,
        execution_timeout=timedelta(seconds=60),
    )
    assert result == "quality_analyzed"
