"""Database fixture ownership and credential-safe failures without a live cluster."""

import asyncio
import inspect
import traceback
from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from tests.integration import conftest as fixtures


def test_successful_readiness_retains_connection_until_caller_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = fixtures.DatabaseEndpoint(
        "127.0.0.1", 15432, "owner", "unit-value", "db"
    )

    class Connection:
        closed = False

        async def execute(self, query: str) -> Any:
            assert query == "SELECT current_database(), current_user, 1"
            return self

        async def fetchone(self) -> tuple[str, str, int]:
            return ("db", "owner", 1)

        async def close(self) -> None:
            self.closed = True

    connection = Connection()

    async def connect(conninfo: str, **kwargs: Any) -> Any:
        assert conninfo == endpoint.conninfo
        assert kwargs["autocommit"] is True
        return connection

    monkeypatch.setattr(fixtures.psycopg.AsyncConnection, "connect", connect)
    retained = fixtures.wait_database(endpoint)
    assert retained is connection
    assert not connection.closed
    asyncio.run(retained.close())
    assert connection.closed


def test_database_connection_failure_hides_driver_credential_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "nonfunctional-" + uuid4().hex
    endpoint = fixtures.DatabaseEndpoint("127.0.0.1", 15432, "owner", password, "db")

    def connect(conninfo: str) -> Any:
        raise fixtures.psycopg.OperationalError(conninfo)

    monkeypatch.setattr(fixtures.psycopg, "connect", connect)
    with pytest.raises(RuntimeError, match="Authenticated PostgreSQL") as error:
        endpoint.connect()
    output = "".join(traceback.format_exception(error.value))
    assert password not in output
    assert password not in repr(endpoint)


def test_each_database_test_gets_a_distinct_owned_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = fixtures.ClusterContract(
        "unit",
        "kind-unit",
        "unit-config",
        "lab",
        "control",
        "substrate",
        "substrate",
        "lab",
        "provider",
        "kind",
    )
    source = fixtures.DatabaseEndpoint("127.0.0.1", 45000, "owner", "unit-value", "db")
    manager = fixtures.ForwardManager(contract, tmp_path)
    environment = cast(
        fixtures.IntegrationEnvironment,
        SimpleNamespace(
            contract=contract,
            database=source,
            forward_manager=manager,
            lifecycle={"databaseService": "control-db", "databasePort": "15432"},
        ),
    )
    started: list[fixtures.ForwardManager] = []
    closed: list[fixtures.ForwardManager] = []

    def start(
        manager: fixtures.ForwardManager, namespace: str, service: str, port: str
    ) -> int:
        assert (namespace, service, port) == ("lab", "control-db", "15432")
        started.append(manager)

        def close() -> None:
            closed.append(manager)

        manager.forwards.append(
            cast(fixtures.OwnedForward, SimpleNamespace(close=close))
        )
        return 46000 + len(started)

    monkeypatch.setattr(fixtures.ForwardManager, "start", start)
    factory = cast(
        Callable[
            [fixtures.IntegrationEnvironment],
            Generator[fixtures.DatabaseEndpoint, None, None],
        ],
        inspect.unwrap(fixtures.database_endpoint),
    )
    ports = []
    for _ in range(2):
        generator = factory(environment)
        endpoint = next(generator)
        assert (endpoint.host, endpoint.user, endpoint.database) == (
            source.host,
            source.user,
            source.database,
        )
        ports.append(endpoint.port)
        generator.close()
    assert ports == [46001, 46002]
    assert started == closed
    assert started[0] is started[1] is environment.forward_manager
