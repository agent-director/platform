import pytest
from temporalio.worker import Worker
from temporalio.testing import WorkflowEnvironment
from orchestrator.temporal_worker import (
    AgentWorkflow,
    run_langgraph_workflow,
    JobInput,
    cleanup_worker_state,
)
import orchestrator.temporal_worker as worker_module
from langgraph.checkpoint.memory import MemorySaver
from unittest.mock import patch, AsyncMock


class MockMemorySaver(MemorySaver):
    async def setup(self):
        pass


@pytest.mark.asyncio
async def test_agent_workflow():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Mock Postgres connection and setup so we don't need a real DB
        with patch(
            "orchestrator.temporal_worker.AsyncPostgresSaver",
            return_value=MockMemorySaver(),
        ):
            with patch("psycopg_pool.AsyncConnectionPool.open", new_callable=AsyncMock):
                await worker_module.init_worker_state()

                # Start worker
                try:
                    # Start worker
                    worker = Worker(
                        env.client,
                        task_queue="test-task-queue",
                        workflows=[AgentWorkflow],
                        activities=[run_langgraph_workflow],
                    )

                    async with worker:
                        # Run ci_pipeline workflow
                        job_input = JobInput(
                            job_id="test-job-1", workflow_type="ci_pipeline"
                        )
                        result = await env.client.execute_workflow(
                            AgentWorkflow.run,
                            job_input,
                            id="test-workflow-1",
                            task_queue="test-task-queue",
                        )
                        assert result == "quality_analyzed"

                        job_input2 = JobInput(
                            job_id="test-job-2", workflow_type="pm_standup"
                        )

                        result2 = await env.client.execute_workflow(
                            AgentWorkflow.run,
                            job_input2,
                            id="test-workflow-2",
                            task_queue="test-task-queue",
                        )
                        assert result2 == "blockers_identified"
                finally:
                    await cleanup_worker_state()
