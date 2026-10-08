from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class PMStandupState(TypedDict):
    job_id: str
    status: str
    blockers: list[str]


def gather_updates(state: PMStandupState) -> dict:
    return {"status": "updates_gathered"}


def identify_blockers(state: PMStandupState) -> dict:
    return {"blockers": ["database migration"], "status": "blockers_identified"}


def build_pm_standup() -> StateGraph:
    workflow = StateGraph(PMStandupState)

    workflow.add_node("gather_updates", gather_updates)
    workflow.add_node("identify_blockers", identify_blockers)

    workflow.add_edge(START, "gather_updates")
    workflow.add_edge("gather_updates", "identify_blockers")
    workflow.add_edge("identify_blockers", END)
    return workflow
