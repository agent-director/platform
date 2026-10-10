from typing import TypedDict

from temporalio import workflow


class PMStandupState(TypedDict):
    job_id: str
    status: str
    blockers: list[str]


@workflow.defn(name="PMStandupWorkflow")
class PMStandupWorkflow:
    @workflow.run
    async def run(self, job_id: str) -> dict:
        # Stub workflow returning hardcoded dict
        return {
            "job_id": job_id,
            "status": "blockers_identified",
            "blockers": ["database migration"],
        }
