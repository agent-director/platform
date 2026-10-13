import asyncio
import signal
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from typing_extensions import override

from orchestrator.temporal_worker import (
    AgentWorkflow,
    CIPipelineWorkflow,
    JobInput,
    PMStandupWorkflow,
    WorkerSettings,
    execute_block,
    health_response,
    main,
    run_worker,
    serve_health,
)


@pytest.mark.asyncio
async def test_agent_and_placeholder_workflows() -> None:
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

            ci_state = await env.client.execute_workflow(
                CIPipelineWorkflow.run,
                "placeholder-ci-job",
                id="placeholder-ci",
                task_queue="test-task-queue",
            )
            assert ci_state == {
                "job_id": "placeholder-ci-job",
                "status": "quality_analyzed",
                "code_quality": 100,
                "tests_passed": True,
            }
            standup_state = await env.client.execute_workflow(
                PMStandupWorkflow.run,
                "placeholder-standup-job",
                id="placeholder-standup",
                task_queue="test-task-queue",
            )
            assert standup_state == {
                "job_id": "placeholder-standup-job",
                "status": "blockers_identified",
                "blockers": ["database migration"],
            }

            job_input3 = JobInput(job_id="test-job-3", workflow_type="invalid_type")
            with pytest.raises(WorkflowFailureError) as exc_info:
                await env.client.execute_workflow(
                    AgentWorkflow.run,
                    job_input3,
                    id="test-workflow-3",
                    task_queue="test-task-queue",
                )
            cause = exc_info.value.cause
            assert cause is not None
            assert "Unknown workflow type" in str(cause.__cause__)


def test_worker_settings_require_explicit_temporal_endpoint() -> None:
    with pytest.raises(ValueError, match="TEMPORAL_URL is required"):
        WorkerSettings.from_environment({})


@pytest.mark.parametrize(
    "endpoint",
    [
        "localhost",
        "http://localhost:7233",
        "localhost:0",
        "localhost:70000",
        "localhost:7233/path",
        "user:pass@localhost:7233",
        "localhost:7233?",
        "localhost:7233#",
    ],
)
def test_worker_settings_reject_invalid_temporal_endpoint(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        WorkerSettings(temporal_url=endpoint)


def test_worker_settings_custom_environment() -> None:
    settings = WorkerSettings.from_environment(
        {
            "TEMPORAL_URL": "custom-temporal.example:8233",
            "TEMPORAL_NAMESPACE": "tenant-test",
            "TEMPORAL_TASK_QUEUE": "custom-queue",
            "WORKER_HEALTH_PORT": "8089",
        }
    )
    assert settings.temporal_url == "custom-temporal.example:8233"
    assert settings.namespace == "tenant-test"
    assert settings.task_queue == "custom-queue"
    assert settings.health_port == 8089


@pytest.mark.asyncio
async def test_worker_health_signals_running_and_temporal_availability() -> None:
    checks = 0

    async def healthy() -> bool:
        nonlocal checks
        checks += 1
        return True

    async def unhealthy() -> bool:
        return False

    async def unavailable() -> bool:
        raise ConnectionError("Temporal unavailable")

    assert await health_response("/ready", False, healthy) == (
        503,
        b"worker not running\n",
    )
    assert checks == 0
    assert await health_response("/health", True, unhealthy) == (200, b"ok\n")
    assert await health_response("/ready", True, healthy) == (200, b"ok\n")
    assert checks == 1
    assert await health_response("/ready", True, unhealthy) == (
        503,
        b"Temporal unavailable\n",
    )
    assert await health_response("/ready", True, unavailable) == (
        503,
        b"Temporal unavailable\n",
    )
    assert await health_response("/missing", True, healthy) == (404, b"not found\n")


class FakeWorker:
    def __init__(
        self, *, startup_error: bool = False, startup_gate: asyncio.Event | None = None
    ) -> None:
        self.startup_error = startup_error
        self.startup_gate = startup_gate
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.validation_started = asyncio.Event()
        self.running = False
        self.shutdown_calls = 0

    @property
    def is_running(self) -> bool:
        return self.running

    async def run(self) -> None:
        self.validation_started.set()
        if self.startup_gate is not None:
            await self.startup_gate.wait()
        if self.startup_error:
            raise ValueError("namespace validation failed")
        self.running = True
        self.started.set()
        await self.stopped.wait()
        self.running = False

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.stopped.set()


async def empty_health_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    writer.close()
    await writer.wait_closed()


async def open_health_server() -> asyncio.Server:
    return await asyncio.start_server(empty_health_connection, "127.0.0.1", 0)


@pytest.mark.asyncio
async def test_worker_startup_failure_propagates_without_shutdown_deadlock() -> None:
    worker = FakeWorker(startup_error=True)
    with pytest.raises(ValueError, match="namespace validation failed"):
        await asyncio.wait_for(
            run_worker(worker, asyncio.Event(), open_health_server()), 1
        )
    assert worker.shutdown_calls == 0
    assert not worker.is_running


@pytest.mark.asyncio
async def test_worker_stop_during_validation_does_not_wait_for_shutdown() -> None:
    worker = FakeWorker(startup_gate=asyncio.Event())
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(worker, stop, open_health_server()))
    await worker.validation_started.wait()
    stop.set()
    await asyncio.wait_for(task, 1)
    assert worker.shutdown_calls == 0
    assert not worker.is_running


@pytest.mark.asyncio
async def test_worker_prestart_cleanup_preserves_supervisor_cancellation() -> None:
    class CancelRaceWorker(FakeWorker):
        @override
        async def run(self) -> None:
            try:
                await super().run()
            finally:
                asyncio.get_running_loop().call_soon(task.cancel)
                await asyncio.sleep(0)

    worker = CancelRaceWorker(startup_gate=asyncio.Event())
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(worker, stop, open_health_server()))
    await worker.validation_started.wait()
    stop.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert task.cancelled()
    assert worker.shutdown_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_running_worker_shuts_down_on_stop_or_cancellation(cancel: bool) -> None:
    worker = FakeWorker()
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(worker, stop, open_health_server()))
    await worker.started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    else:
        stop.set()
        await asyncio.wait_for(task, 1)
    assert worker.shutdown_calls == 1
    assert not worker.is_running


@pytest.mark.asyncio
async def test_worker_ready_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestrator.temporal_worker.HEALTH_TIMEOUT_SECONDS", 0.01)

    async def unavailable() -> bool:
        await asyncio.Event().wait()
        return True

    assert await health_response("/ready", True, unavailable) == (
        503,
        b"Temporal unavailable\n",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_line,status,body",
    [
        (b"GET /health HTTP/1.1", 200, b"ok\n"),
        (b"GET /ready HTTP/1.1", 200, b"ok\n"),
        (b"GET /missing HTTP/1.1", 404, b"not found\n"),
        (b"POST /health HTTP/1.1", 400, b"bad request\n"),
    ],
)
async def test_worker_health_http_responses(
    request_line: bytes, status: int, body: bytes
) -> None:
    worker = FakeWorker()
    worker.running = True

    async def healthy() -> bool:
        return True

    async def respond(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await serve_health(reader, writer, worker, healthy)

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        assert server.sockets is not None
        port = server.sockets[0].getsockname()[1]
        assert isinstance(port, int)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request_line + b"\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 1)
        writer.close()
        await writer.wait_closed()
    assert response.startswith(f"HTTP/1.1 {status} ".encode())
    assert response.endswith(b"\r\n\r\n" + body)
    assert f"Content-Length: {len(body)}".encode() in response


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_fails", [False, True])
async def test_worker_main_injects_settings_and_cleans_signal_handlers(
    binding_fails: bool,
) -> None:
    from collections.abc import Callable

    settings = WorkerSettings(
        temporal_url="custom-temporal.test:8233",
        namespace="custom-namespace",
        task_queue="custom-queue",
        health_port=8089,
    )
    worker = FakeWorker()
    loop = asyncio.get_running_loop()
    real_start_server = asyncio.start_server

    def register(stop_signal: int, callback: Callable[[], None]) -> None:
        if stop_signal == signal.SIGTERM:
            callback()

    async def bind_health(*args: object, **kwargs: object) -> asyncio.Server:
        if binding_fails:
            raise OSError("health bind failed")
        return await real_start_server(empty_health_connection, "127.0.0.1", 0)

    with (
        patch(
            "orchestrator.temporal_worker.Client.connect", new_callable=AsyncMock
        ) as connect,
        patch(
            "orchestrator.temporal_worker.Worker", return_value=worker
        ) as create_worker,
        patch(
            "orchestrator.temporal_worker.asyncio.start_server", new_callable=AsyncMock
        ) as bind,
        patch.object(loop, "add_signal_handler", side_effect=register),
        patch.object(loop, "remove_signal_handler") as remove_signal,
    ):
        connect.return_value = Mock()
        bind.side_effect = bind_health
        if binding_fails:
            with pytest.raises(OSError, match="health bind failed"):
                await main(settings)
        else:
            await main(settings)
        connect.assert_awaited_once_with(
            "custom-temporal.test:8233", namespace="custom-namespace"
        )
        assert create_worker.call_args.kwargs["task_queue"] == "custom-queue"
        assert bind.call_args.kwargs["port"] == 8089
        assert bind.call_args.kwargs["host"] == "0.0.0.0"
        assert {call.args[0] for call in remove_signal.call_args_list} == {
            signal.SIGINT,
            signal.SIGTERM,
        }
    assert not worker.is_running
