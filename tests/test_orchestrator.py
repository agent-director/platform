import pytest
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.temporal_worker import (
    AgentWorkflow,
    CIPipelineWorkflow,
    JobInput,
    PMStandupWorkflow,
    execute_block,
)


@pytest.mark.asyncio
async def test_agent_workflow():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[AgentWorkflow, CIPipelineWorkflow, PMStandupWorkflow],
            activities=[execute_block],
        )

        async with worker:
            job_input = JobInput(job_id="test-job-1", workflow_type="ci_pipeline")
            result = await env.client.execute_workflow(
                AgentWorkflow.run,
                job_input,
                id="test-workflow-1",
                task_queue="test-task-queue",
            )
            assert result == "quality_analyzed"

            job_input2 = JobInput(job_id="test-job-2", workflow_type="pm_standup")
            result2 = await env.client.execute_workflow(
                AgentWorkflow.run,
                job_input2,
                id="test-workflow-2",
                task_queue="test-task-queue",
            )
            assert result2 == "blockers_identified"

            job_input3 = JobInput(job_id="test-job-3", workflow_type="invalid_type")
            with pytest.raises(WorkflowFailureError) as exc_info:
                await env.client.execute_workflow(
                    AgentWorkflow.run,
                    job_input3,
                    id="test-workflow-3",
                    task_queue="test-task-queue",
                )
            assert "Unknown workflow type" in str(exc_info.value.cause.cause)
