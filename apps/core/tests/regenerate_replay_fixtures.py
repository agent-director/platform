"""Regenerate the committed Temporal Replayer history fixtures.

Run after an intentional workflow change (new commands, signals or ordering):

    make replay-fixtures
    make replay-fixtures REPLAY_ARGS="--only agent_workflow_signal_history.json"

Review the JSON diff before committing. Uses the SDK time-skipping test server
(downloaded once, then cached). Histories are normalized (worker identity, run IDs,
event times and per-run server metadata), so output is deterministic and a fixture
only changes when its recorded history content changes.
"""

import argparse
import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from temporalio.client import Client, WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.temporal_worker import (
    AgentWorkflow,
    CIPipelineWorkflow,
    JobInput,
    PMStandupWorkflow,
    execute_block,
)

FIXTURES_DIR = Path(__file__).parent / "unit" / "fixtures"
TASK_QUEUE = "test-task-queue"
IDENTITY = "replay-fixture"
EVENT_TIME_BASE = datetime(2026, 1, 1, tzinfo=UTC)

IDENTITY_PATTERN = re.compile(r'"identity": "[^"]*"')
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

# Server and worker metadata that varies per run and is never read during replay.
STABLE_VALUES = {"historySizeBytes": "0", "binaryChecksum": "0" * 32}
# Serialized from sets, so their order differs between runs.
SORTED_LISTS = {"coreUsedFlags", "langUsedFlags"}

Scenario = Callable[[Client], Awaitable[WorkflowHandle[Any, Any]]]


async def agent_ci(client: Client) -> WorkflowHandle[Any, Any]:
    return await client.start_workflow(
        AgentWorkflow.run,
        JobInput(job_id="replay-ci-1", workflow_type="ci_pipeline"),
        id="wf-replay-agent-ci",
        task_queue=TASK_QUEUE,
    )


async def agent_pm(client: Client) -> WorkflowHandle[Any, Any]:
    return await client.start_workflow(
        AgentWorkflow.run,
        JobInput(job_id="replay-pm-1", workflow_type="pm_standup"),
        id="wf-replay-agent-pm",
        task_queue=TASK_QUEUE,
    )


async def agent_signal(client: Client) -> WorkflowHandle[Any, Any]:
    # The signal is delivered with the start, so update_status runs before the
    # activity result is applied and the workflow returns "paused".
    return await client.start_workflow(
        AgentWorkflow.run,
        JobInput(job_id="replay-signal-1", workflow_type="pm_standup"),
        id="wf-replay-agent-signal",
        task_queue=TASK_QUEUE,
        start_signal="update_status",
        start_signal_args=["paused"],
    )


async def ci_pipeline(client: Client) -> WorkflowHandle[Any, Any]:
    return await client.start_workflow(
        CIPipelineWorkflow.run,
        "replay-ci-direct-1",
        id="wf-replay-ci-direct",
        task_queue=TASK_QUEUE,
    )


async def pm_standup(client: Client) -> WorkflowHandle[Any, Any]:
    return await client.start_workflow(
        PMStandupWorkflow.run,
        "replay-pm-direct-1",
        id="wf-replay-pm-direct",
        task_queue=TASK_QUEUE,
    )


SCENARIOS: dict[str, Scenario] = {
    "agent_workflow_ci_history.json": agent_ci,
    "agent_workflow_pm_history.json": agent_pm,
    "agent_workflow_signal_history.json": agent_signal,
    "ci_pipeline_workflow_history.json": ci_pipeline,
    "pm_standup_workflow_history.json": pm_standup,
}


def stabilize(node: Any) -> Any:
    if isinstance(node, dict):
        stable = {
            key: STABLE_VALUES.get(
                key, sorted(value) if key in SORTED_LISTS else stabilize(value)
            )
            for key, value in node.items()
        }
        if "eventId" in node and "eventTime" in node:
            moment = EVENT_TIME_BASE + timedelta(seconds=int(node["eventId"]))
            stable["eventTime"] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        return stable
    if isinstance(node, list):
        return [stabilize(item) for item in node]
    return node


def normalize(history_json: str) -> str:
    """Replace per-run values with stable ones, preserving event ordering."""
    text = json.dumps(json.loads(history_json))
    text = IDENTITY_PATTERN.sub(f'"identity": "{IDENTITY}"', text)
    run_ids = dict.fromkeys(UUID_PATTERN.findall(text))
    for index, run_id in enumerate(run_ids, start=1):
        text = text.replace(run_id, f"00000000-0000-4000-8000-{index:012x}")
    return json.dumps(stabilize(json.loads(text)), indent=2) + "\n"


async def write_history(handle: WorkflowHandle[Any, Any], filename: str) -> None:
    await handle.result()
    history = await handle.fetch_history()
    (FIXTURES_DIR / filename).write_text(normalize(history.to_json()), encoding="utf-8")


async def main(selected: list[str]) -> None:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[AgentWorkflow, CIPipelineWorkflow, PMStandupWorkflow],
            activities=[execute_block],
        ),
    ):
        for filename in selected:
            await write_history(await SCENARIOS[filename](env.client), filename)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Regenerate the committed Temporal Replayer history fixtures."
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=sorted(SCENARIOS),
        help="regenerate only this fixture (repeatable); default is every fixture",
    )
    arguments = parser.parse_args()
    asyncio.run(main(arguments.only or sorted(SCENARIOS)))
