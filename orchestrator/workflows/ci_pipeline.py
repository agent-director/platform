from typing import TypedDict

from temporalio import workflow


class CIPipelineState(TypedDict):
    job_id: str
    status: str
    code_quality: int
    tests_passed: bool


@workflow.defn(name="CIPipelineWorkflow")
class CIPipelineWorkflow:
    @workflow.run
    async def run(self, job_id: str) -> dict:
        # Stub workflow returning hardcoded dict
        return {
            "job_id": job_id,
            "status": "quality_analyzed",
            "code_quality": 100,
            "tests_passed": True,
        }
