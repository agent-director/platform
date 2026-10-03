import asyncio
import os
from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from orchestrator.workflows.ci_pipeline import CIPipelineWorkflow
from orchestrator.workflows.pm_standup import PMStandupWorkflow


@dataclass
class JobInput:
    job_id: str
    workflow_type: str


@activity.defn
async def execute_block(job_input: JobInput) -> str:
    """
    Emulates AWS Strands dispatch pattern.
    """
    if job_input.workflow_type == "ci_pipeline":
        return "quality_analyzed"
    elif job_input.workflow_type == "pm_standup":
        return "blockers_identified"
    else:
        raise ApplicationError(
            f"Unknown workflow type: {job_input.workflow_type}",
            type="ValueError",
            non_retryable=True,
        )


@workflow.defn(sandboxed=False)
class AgentWorkflow:
    def __init__(self) -> None:
        self.status = "started"

    @workflow.run
    async def run(self, job_input: JobInput) -> str:
        # Schedule the activity
        result = await workflow.execute_activity(
            execute_block,
            job_input,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(non_retryable_error_types=["ValueError"]),
        )
        if self.status in ["started", "processing"]:
            self.status = result
        return self.status

    @workflow.signal
    def update_status(self, new_status: str) -> None:
        if new_status in ["started", "processing", "paused", "waiting_on_human"]:
            self.status = new_status


async def main():
    temporal_url = os.environ.get("TEMPORAL_URL", "localhost:7233")
    client = await Client.connect(temporal_url)
    worker = Worker(
        client,
        task_queue="agent-task-queue",
        workflows=[AgentWorkflow, CIPipelineWorkflow, PMStandupWorkflow],
        activities=[execute_block],
    )
    print("Starting worker...")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
