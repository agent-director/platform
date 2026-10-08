from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class CIPipelineState(TypedDict):
    job_id: str
    status: str
    code_quality: int
    tests_passed: bool


def fetch_code(state: CIPipelineState) -> dict:
    return {"status": "code_fetched"}


def run_tests(state: CIPipelineState) -> dict:
    # simulate tests
    return {"tests_passed": True, "status": "tests_run"}


def analyze_quality(state: CIPipelineState) -> dict:
    return {"code_quality": 100, "status": "quality_analyzed"}


def build_ci_pipeline() -> StateGraph:
    workflow = StateGraph(CIPipelineState)

    workflow.add_node("fetch_code", fetch_code)
    workflow.add_node("run_tests", run_tests)
    workflow.add_node("analyze_quality", analyze_quality)

    workflow.add_edge(START, "fetch_code")
    workflow.add_edge("fetch_code", "run_tests")
    workflow.add_edge("run_tests", "analyze_quality")
    workflow.add_edge("analyze_quality", END)
    return workflow
