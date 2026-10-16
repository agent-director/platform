import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from orchestrator.workflows.ci_pipeline import CIPipelineWorkflow
from orchestrator.workflows.pm_standup import PMStandupWorkflow

HEALTH_TIMEOUT_SECONDS = 3.0


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


class WorkerSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    temporal_url: str
    namespace: str = Field(default="default", min_length=1)
    task_queue: str = Field(default="agent-task-queue", min_length=1)
    health_port: int = Field(default=8081, ge=1, le=65535)

    @field_validator("temporal_url")
    @classmethod
    def validate_temporal_url(cls, value: str) -> str:
        parsed = urlsplit(f"//{value}")
        if (
            any(char.isspace() for char in value)
            or not parsed.hostname
            or parsed.port is None
            or parsed.port == 0
            or parsed.path
            or "?" in value
            or "#" in value
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "TEMPORAL_URL must be a host:port address without a scheme"
            )
        return value

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "WorkerSettings":
        env = os.environ if environ is None else environ
        endpoint = env.get("TEMPORAL_URL")
        if not endpoint:
            raise ValueError("TEMPORAL_URL is required")
        return cls(
            temporal_url=endpoint,
            namespace=env.get("TEMPORAL_NAMESPACE", "default"),
            task_queue=env.get("TEMPORAL_TASK_QUEUE", "agent-task-queue"),
            health_port=int(env.get("WORKER_HEALTH_PORT", "8081")),
        )


async def health_response(
    path: str,
    is_running: bool,
    check_temporal: Callable[[], Awaitable[bool]],
) -> tuple[int, bytes]:
    """Liveness tracks worker startup; readiness also checks Temporal availability."""
    if path not in {"/health", "/ready"}:
        return 404, b"not found\n"
    if not is_running:
        return 503, b"worker not running\n"
    if path == "/ready":
        try:
            healthy = await asyncio.wait_for(
                check_temporal(), timeout=HEALTH_TIMEOUT_SECONDS
            )
        except Exception:  # noqa: BLE001 -- readiness must fail closed for any SDK callback failure
            healthy = False
        if not healthy:
            return 503, b"Temporal unavailable\n"
    return 200, b"ok\n"


async def serve_health(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    worker: "RunnableWorker",
    check_temporal: Callable[[], Awaitable[bool]],
) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=3)
        parts = request_line.decode("ascii", errors="replace").strip().split()
        if len(parts) != 3 or parts[0] != "GET":
            status, body = 400, b"bad request\n"
        else:
            status, body = await health_response(
                parts[1], worker.is_running, check_temporal
            )
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            503: "Service Unavailable",
        }[status]
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
    except (OSError, TimeoutError, ValueError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


class RunnableWorker(Protocol):
    @property
    def is_running(self) -> bool: ...

    async def run(self) -> None: ...

    async def shutdown(self) -> None: ...


async def run_worker(
    worker: RunnableWorker,
    stop: asyncio.Event,
    health_server: Awaitable[asyncio.AbstractServer],
) -> None:
    """Propagate startup failures without waiting on an unreachable SDK shutdown event."""
    worker_task = asyncio.create_task(worker.run())
    stop_task = asyncio.create_task(stop.wait())
    try:
        async with await health_server:
            done, _ = await asyncio.wait(
                (worker_task, stop_task), return_when=asyncio.FIRST_COMPLETED
            )
            if worker_task in done:
                await worker_task
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        if not worker_task.done() and not worker.is_running:
            # SDK namespace validation occurs before it can signal shutdown completion.
            worker_task.cancel()
            result = (await asyncio.gather(worker_task, return_exceptions=True))[0]
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                raise result
        else:
            if not worker_task.done():
                await worker.shutdown()
            await worker_task


async def main(settings: WorkerSettings | None = None) -> None:
    settings = settings or WorkerSettings.from_environment()
    client = await Client.connect(settings.temporal_url, namespace=settings.namespace)
    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[AgentWorkflow, CIPipelineWorkflow, PMStandupWorkflow],
        activities=[execute_block],
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(stop_signal, stop.set)

    async def handle_health(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await serve_health(reader, writer, worker, client.service_client.check_health)

    try:
        await run_worker(
            worker,
            stop,
            asyncio.start_server(
                handle_health, host="0.0.0.0", port=settings.health_port, limit=4096
            ),
        )
    finally:
        for stop_signal in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(stop_signal)


if __name__ == "__main__":
    asyncio.run(main())
