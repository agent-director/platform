"""Real-cluster fixtures: chart-owned endpoints and session-owned random forwards."""

import asyncio
import base64
import os
import re
import signal
import subprocess
import time
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any, TextIO, cast

import httpx
import psycopg
import pytest
import pytest_asyncio
import yaml
from psycopg.conninfo import make_conninfo
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client

REPO_ROOT = Path(__file__).absolute().parents[2]
Resource = dict[str, Any]


def run_tool(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(REPO_ROOT / "scripts" / "mise.sh"), "exec", "--", *arguments],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )


@dataclass(frozen=True)
class ClusterContract:
    cluster_name: str
    context: str
    kubeconfig: str
    namespace: str
    release: str
    substrate_namespace: str
    substrate_release: str
    provider_namespace: str
    provider_release: str
    profile: str

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "ClusterContract":
        keys = (
            "CLUSTER_NAME",
            "KUBE_CONTEXT",
            "KUBECONFIG",
            "NAMESPACE",
            "RELEASE_NAME",
            "SUBSTRATE_NAMESPACE",
            "SUBSTRATE_RELEASE_NAME",
            "PROVIDER_NAMESPACE",
            "PROVIDER_RELEASE_NAME",
            "PROFILE",
        )
        missing = [key for key in keys if not environ.get(key)]
        if missing:
            raise ValueError(f"Integration contract is missing: {', '.join(missing)}")
        values = [environ[key] for key in keys]
        contract = cls(*values)
        if contract.context != f"kind-{contract.cluster_name}":
            raise ValueError("Integration CLUSTER_NAME and KUBE_CONTEXT disagree")
        expected_config = (
            REPO_ROOT / ".cache" / "clusters" / contract.cluster_name / "kubeconfig"
        )
        if Path(contract.kubeconfig).absolute() != expected_config:
            raise ValueError(
                "Integration requires the selected cluster's dedicated repository-local KUBECONFIG"
            )
        if contract.profile not in {"kind", "full"}:
            raise ValueError("Integration PROFILE must be kind or full")
        return contract

    def kubectl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return run_tool(
            "kubectl",
            "--context",
            self.context,
            "--kubeconfig",
            self.kubeconfig,
            *arguments,
        )

    def manifest(self, release: str, namespace: str) -> list[Resource]:
        result = run_tool(
            "helm",
            "get",
            "manifest",
            release,
            "--namespace",
            namespace,
            "--kube-context",
            self.context,
            "--kubeconfig",
            self.kubeconfig,
        )
        return parse_resources(result.stdout)


def parse_resources(text: str) -> list[Resource]:
    resources: list[Resource] = []
    for document in yaml.safe_load_all(text):
        if document is None:
            continue
        if not isinstance(document, dict) or not document.get("kind"):
            raise ValueError("Helm manifest contains an invalid resource")
        resources.append(cast(Resource, document))
    if not resources:
        raise ValueError("Helm manifest is empty; refusing reduced smoke coverage")
    return resources


def lifecycle_values(resources: Sequence[Resource], release: str) -> dict[str, str]:
    contracts = [
        resource
        for resource in resources
        if resource["kind"] == "ConfigMap"
        and resource.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "lifecycle-contract"
        and resource.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/instance")
        == release
    ]
    if len(contracts) != 1:
        raise ValueError("Expected exactly one chart-owned lifecycle contract")
    data = contracts[0].get("data")
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise ValueError("Lifecycle contract must contain string data")
    return cast(dict[str, str], data)


def required_value(values: Mapping[str, str], key: str) -> str:
    value = values.get(key)
    if not value:
        raise ValueError(f"Chart lifecycle contract is missing {key}")
    return value


@dataclass(frozen=True)
class Workload:
    namespace: str
    kind: str
    name: str
    selector: str


def discover_workloads(resources: Sequence[Resource], namespace: str) -> list[Workload]:
    workloads: list[Workload] = []
    for resource in resources:
        if resource["kind"] not in {"Deployment", "StatefulSet"}:
            continue
        metadata = resource.get("metadata", {})
        match_labels = resource.get("spec", {}).get("selector", {}).get("matchLabels")
        if (
            not metadata.get("name")
            or not isinstance(match_labels, dict)
            or not match_labels
        ):
            raise ValueError(
                "Rendered workload has no name or selector; smoke discovery failed"
            )
        workloads.append(
            Workload(
                metadata.get("namespace", namespace),
                resource["kind"],
                metadata["name"],
                ",".join(
                    f"{key}={value}" for key, value in sorted(match_labels.items())
                ),
            )
        )
    if not workloads:
        raise ValueError(
            f"No workloads discovered in {namespace}; refusing reduced smoke coverage"
        )
    return workloads


@dataclass
class OwnedForward:
    process: subprocess.Popen[str]
    log: TextIO
    path: Path
    port: int = 0

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=5)
        finally:
            self.log.close()


class ForwardManager:
    """Tracks exactly the kubectl children this fixture started, including failed starts."""

    def __init__(self, contract: ClusterContract, directory: Path) -> None:
        self.contract = contract
        self.directory = directory
        self.forwards: list[OwnedForward] = []
        self._starting = False
        self._pending_signal: int | None = None

    def start(
        self, namespace: str, service: str, remote_port: str, timeout: float = 30
    ) -> int:
        path = self.directory / f"forward-{len(self.forwards)}.log"
        log = path.open("w", encoding="utf-8")
        process: subprocess.Popen[str] | None = None
        forward: OwnedForward | None = None
        # Defer our handlers during spawn-and-register. Blocking POSIX signals
        # would also pass a blocked signal mask into kubectl, so do not do that.
        self._starting = True
        try:
            process = subprocess.Popen(
                [
                    str(REPO_ROOT / "scripts" / "mise.sh"),
                    "exec",
                    "--",
                    "kubectl",
                    "--context",
                    self.contract.context,
                    "--kubeconfig",
                    self.contract.kubeconfig,
                    "--namespace",
                    namespace,
                    "port-forward",
                    "--address",
                    "127.0.0.1",
                    f"service/{service}",
                    f":{remote_port}",
                    "--pod-running-timeout=30s",
                ],
                cwd=REPO_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            forward = OwnedForward(process, log, path)
            self.forwards.append(forward)
        except BaseException:
            if process is not None:
                OwnedForward(process, log, path).close()
            else:
                log.close()
            raise
        finally:
            self._starting = False
            pending = self._pending_signal
            self._pending_signal = None
            if pending is not None:
                self.handle_signal(pending, None)
        assert forward is not None and process is not None
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                output = path.read_text(encoding="utf-8")
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+) ->", output)
                if match and process.poll() is None:
                    forward.port = int(match.group(1))
                    return forward.port
                if process.poll() is not None:
                    raise RuntimeError(f"Port-forward to {service} exited: {output}")
                time.sleep(0.05)
            raise TimeoutError(
                f"Port-forward to {service} did not become ready: {path.read_text()}"
            )
        except BaseException:
            forward.close()
            raise

    @contextmanager
    def scoped_forward(
        self, namespace: str, service: str, remote_port: str
    ) -> Generator[int, None, None]:
        # Use the session's signal-aware spawn registration, but close only this
        # test's forward on normal teardown. Signals still close the entire registry.
        port = self.start(namespace, service, remote_port)
        forward = self.forwards[-1]
        try:
            yield port
        finally:
            forward.close()

    def close(self) -> None:
        first_error: Exception | None = None
        for forward in reversed(self.forwards):
            try:
                forward.close()
            except Exception as error:  # noqa: BLE001 -- close every owned forward before re-raising
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        if self._starting:
            self._pending_signal = signum
            return
        self.close()
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)


@dataclass(frozen=True)
class DatabaseEndpoint:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str

    @property
    def conninfo(self) -> str:
        return make_conninfo(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=self.database,
            sslmode="require",
            connect_timeout=5,
            options="-c statement_timeout=5000 -c lock_timeout=5000",
        )

    def connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        try:
            return psycopg.connect(self.conninfo)
        except psycopg.Error:
            # Driver tracebacks include conninfo/password arguments in pytest output.
            raise RuntimeError("Authenticated PostgreSQL connection failed") from None


@dataclass(frozen=True)
class SubstrateEndpoint:
    address: str
    port: int
    server_name: str
    ca_file: Path


@dataclass(frozen=True)
class IntegrationEnvironment:
    contract: ClusterContract
    lifecycle: dict[str, str]
    workloads: list[Workload]
    api_url: str
    database: DatabaseEndpoint
    temporal_address: str
    temporal_namespace: str
    temporal_task_queue: str
    substrate: SubstrateEndpoint
    forward_manager: ForwardManager = field(repr=False)


def secret_data(contract: ClusterContract, namespace: str, name: str) -> dict[str, str]:
    import json

    resource = json.loads(
        contract.kubectl(
            "get", "secret", name, "--namespace", namespace, "-o", "json"
        ).stdout
    )
    data = resource.get("data")
    if not isinstance(data, dict) or not data:
        raise ValueError(f"Secret {name} has no data")
    return {
        key: base64.b64decode(value, validate=True).decode("utf-8")
        for key, value in data.items()
    }


def substrate_service(resources: Sequence[Resource]) -> tuple[str, str, str]:
    """Discover the TLS gRPC Service and its mounted trust Secret from actual manifests."""
    component = "ateapi"
    services = [
        resource
        for resource in resources
        if resource["kind"] == "Service"
        and resource.get("spec", {})
        .get("selector", {})
        .get("app.kubernetes.io/component")
        == component
    ]
    deployments = [
        resource
        for resource in resources
        if resource["kind"] == "Deployment"
        and resource.get("spec", {})
        .get("selector", {})
        .get("matchLabels", {})
        .get("app.kubernetes.io/component")
        == component
    ]
    if len(services) != 1 or len(deployments) != 1:
        raise ValueError("Expected exactly one substrate API Service and Deployment")
    ports = [
        port
        for port in services[0]["spec"].get("ports", [])
        if port.get("name") == "grpc"
    ]
    volumes = deployments[0]["spec"]["template"]["spec"].get("volumes", [])
    tls = [volume for volume in volumes if volume.get("name") == "tls"]
    if (
        len(ports) != 1
        or len(tls) != 1
        or not tls[0].get("secret", {}).get("secretName")
    ):
        raise ValueError("Substrate gRPC port or TLS Secret missing from chart")
    return (
        services[0]["metadata"]["name"],
        str(ports[0]["port"]),
        tls[0]["secret"]["secretName"],
    )


def wait_api(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{url}/health", timeout=2)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise TimeoutError(
        "Unified API did not return successful health over its owned forward"
    )


def wait_database(
    endpoint: DatabaseEndpoint, timeout: float = 30
) -> psycopg.AsyncConnection[tuple[Any, ...]]:
    async def probe() -> psycopg.AsyncConnection[tuple[Any, ...]] | None:
        connection = await psycopg.AsyncConnection.connect(
            endpoint.conninfo, autocommit=True
        )
        try:
            cursor = await connection.execute(
                "SELECT current_database(), current_user, 1"
            )
            if await cursor.fetchone() == (endpoint.database, endpoint.user, 1):
                return connection
        except BaseException:
            await connection.close()
            raise
        await connection.close()
        return None

    async def wait_ready() -> psycopg.AsyncConnection[tuple[Any, ...]]:
        # Keep the successful read-only session until teardown: libpq TLS close
        # can terminate the whole kubectl tunnel (kubernetes/kubectl#1746).
        async with asyncio.timeout(timeout):
            while True:
                try:
                    connection = await probe()
                    if connection is not None:
                        return connection
                except psycopg.Error:
                    pass
                await asyncio.sleep(0.1)

    try:
        return asyncio.run(wait_ready())
    except TimeoutError:
        raise TimeoutError(
            "Authenticated PostgreSQL query failed over its owned forward"
        ) from None


@pytest.fixture(scope="session")
def integration_env(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[IntegrationEnvironment]:
    contract = ClusterContract.from_environment(os.environ)
    platform = contract.manifest(contract.release, contract.namespace)
    substrate = contract.manifest(
        contract.substrate_release, contract.substrate_namespace
    )
    provider = contract.manifest(contract.provider_release, contract.provider_namespace)
    values = lifecycle_values(platform, contract.release)
    if required_value(values, "temporalProfile") != contract.profile:
        raise ValueError(
            "Installed Temporal profile does not match integration PROFILE"
        )
    if required_value(values, "workersEnabled") != "true":
        raise ValueError(
            "Integration requires the completed deployment with workers enabled"
        )
    workloads = (
        discover_workloads(platform, contract.namespace)
        + discover_workloads(substrate, contract.substrate_namespace)
        + discover_workloads(provider, contract.provider_namespace)
    )
    directory = tmp_path_factory.mktemp("integration-endpoints")
    manager = ForwardManager(contract, directory)
    readiness_connection: psycopg.AsyncConnection[tuple[Any, ...]] | None = None
    environment = pytest.MonkeyPatch()
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    for signum in previous_handlers:
        signal.signal(signum, manager.handle_signal)
    try:
        db_port = manager.start(
            contract.namespace,
            required_value(values, "databaseService"),
            required_value(values, "databasePort"),
        )
        credentials = secret_data(
            contract,
            contract.namespace,
            required_value(values, "databaseCredentialSecret"),
        )
        database = DatabaseEndpoint(
            "127.0.0.1",
            db_port,
            credentials["username"],
            credentials["password"],
            required_value(values, "databaseName"),
        )
        readiness_connection = wait_database(database)
        api_port = manager.start(
            contract.namespace,
            required_value(values, "apiService"),
            required_value(values, "apiPort"),
        )
        api_url = f"http://127.0.0.1:{api_port}"
        wait_api(api_url)
        temporal_port = manager.start(
            contract.namespace,
            required_value(values, "temporalService"),
            required_value(values, "temporalPort"),
        )
        temporal_address = f"127.0.0.1:{temporal_port}"
        temporal_namespace = required_value(values, "temporalNamespace")
        temporal_task_queue = required_value(values, "temporalTaskQueue")
        service, grpc_port, tls_secret = substrate_service(substrate)
        substrate_port = manager.start(contract.substrate_namespace, service, grpc_port)
        ca_file = directory / "substrate-ca.pem"
        ca_file.write_text(
            secret_data(contract, contract.substrate_namespace, tls_secret)["ca.crt"],
            encoding="utf-8",
        )
        substrate_endpoint = SubstrateEndpoint(
            "127.0.0.1",
            substrate_port,
            f"{service}.{contract.substrate_namespace}.svc.cluster.local",
            ca_file,
        )
        exports = {
            "API_URL": api_url,
            "DATABASE_URL": database.conninfo,
            "TEMPORAL_ADDRESS": temporal_address,
            "TEMPORAL_NAMESPACE": temporal_namespace,
            "TEMPORAL_TASK_QUEUE": temporal_task_queue,
            "SUBSTRATE_ADDRESS": f"127.0.0.1:{substrate_port}",
            "SUBSTRATE_TLS_SERVER_NAME": substrate_endpoint.server_name,
            "SUBSTRATE_CA_FILE": str(ca_file),
        }
        for key, value in exports.items():
            environment.setenv(key, value)
        yield IntegrationEnvironment(
            contract,
            values,
            workloads,
            api_url,
            database,
            temporal_address,
            temporal_namespace,
            temporal_task_queue,
            substrate_endpoint,
            manager,
        )
    finally:
        try:
            if readiness_connection is not None:
                asyncio.run(readiness_connection.close())
        finally:
            try:
                manager.close()
            finally:
                environment.undo()
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)


@pytest.fixture
def api_url(integration_env: IntegrationEnvironment) -> str:
    return integration_env.api_url


@pytest.fixture
def database_endpoint(
    integration_env: IntegrationEnvironment,
) -> Iterator[DatabaseEndpoint]:
    # Each SQL test owns its tunnel; closing a TLS session must not break another
    # test or the session's retained readiness/export connection.
    with integration_env.forward_manager.scoped_forward(
        integration_env.contract.namespace,
        required_value(integration_env.lifecycle, "databaseService"),
        required_value(integration_env.lifecycle, "databasePort"),
    ) as port:
        source = integration_env.database
        yield DatabaseEndpoint(
            source.host, port, source.user, source.password, source.database
        )


@pytest_asyncio.fixture
async def temporal_client(integration_env: IntegrationEnvironment) -> Client:
    from datetime import timedelta

    client = await asyncio.wait_for(
        Client.connect(
            integration_env.temporal_address,
            namespace=integration_env.temporal_namespace,
        ),
        timeout=30,
    )
    await client.workflow_service.describe_namespace(
        DescribeNamespaceRequest(namespace=integration_env.temporal_namespace),
        timeout=timedelta(seconds=10),
    )
    return client
