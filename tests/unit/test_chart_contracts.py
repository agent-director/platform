"""Independent rendered contracts; these tests never deploy or run containers."""

import base64
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization

Manifest = dict[str, Any]
EMPTY_ITEMS: tuple[object, ...] = ()
EMPTY_MAP: Mapping[str, object] = {}
RELEASE = "raven"
NAMESPACE = "lab-two"


def render(
    chart: Path,
    settings: Mapping[str, str | int | bool] | None = None,
    *,
    values_yaml: str | None = None,
    upgrade: bool = False,
    release: str = RELEASE,
) -> list[Manifest]:
    command = ["helm", "template", release, str(chart), "--namespace", NAMESPACE]
    if upgrade:
        command.append("--is-upgrade")
    if values_yaml is not None:
        command.extend(["-f", "-"])
    for key, value in (settings or {}).items():
        serialized = str(value).lower() if isinstance(value, bool) else str(value)
        command.extend(["--set", f"{key}={serialized}"])
    result = subprocess.run(
        command, input=values_yaml, text=True, capture_output=True, check=True
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def resource(documents: list[Manifest], kind: str, name: str) -> Manifest:
    return next(
        document
        for document in documents
        if document["kind"] == kind and document["metadata"]["name"] == name
    )


def lifecycle(documents: list[Manifest]) -> Manifest:
    return next(
        document
        for document in documents
        if document["kind"] == "ConfigMap"
        and document["metadata"]
        .get("labels", EMPTY_MAP)
        .get("app.kubernetes.io/component")
        == "lifecycle-contract"
    )


def environment(container: Manifest) -> dict[str, Any]:
    return {
        item["name"]: item.get("value", item.get("valueFrom"))
        for item in container.get("env", EMPTY_ITEMS)
    }


def test_bootstrap_preserves_prerequisites_and_suppresses_runtime(
    chart_dir: str,
) -> None:
    documents = render(
        Path(chart_dir), {"bootstrap.enabled": True, "workers.enabled": True}
    )
    deployments = [
        document["metadata"]["name"]
        for document in documents
        if document["kind"] == "Deployment"
    ]
    assert all(
        document["metadata"]["labels"]["app.kubernetes.io/instance"] == RELEASE
        for document in documents
        if document["kind"] == "Deployment"
    )
    assert deployments == ["raven-platform-rustfs"]
    postgres = resource(documents, "postgresql", "raven-db")
    assert postgres["spec"]["databases"]["agent_substrate"] == "agent_substrate"
    assert all(not flags for flags in postgres["spec"]["users"].values()), (
        "Application database roles must not be superusers"
    )
    resource(documents, "ServiceAccount", "raven-platform-temporal-schema")
    resource(documents, "Secret", "raven-db-tls")
    trust = resource(documents, "ConfigMap", "raven-db-trust")
    assert set(trust["data"]) == {"ca.crt"}
    assert "PRIVATE KEY" not in trust["data"]["ca.crt"]
    contract = lifecycle(documents)["data"]
    assert contract["temporalProfile"] == "bootstrap"
    assert contract["workersEnabled"] == "false"
    assert contract["databaseService"] == "raven-db"
    assert contract["databaseName"] == "platform"
    assert (
        contract["substrateDatabaseCredentialSecret"]
        == "agent-substrate.raven-db.credentials.postgresql.acid.zalan.do"
    )
    assert "PRIVATE KEY" not in contract["temporal-values.yaml"]
    assert not any(document["kind"] == "Job" for document in documents)


def test_nondefault_names_ports_and_consumers(chart_dir: str) -> None:
    documents = render(
        Path(chart_dir),
        {
            "fullnameOverride": "control-plane",
            "temporalDev.enabled": True,
            "workers.enabled": True,
            "unifiedApi.service.port": 8189,
            "frontend.service.port": 3339,
            "litellm.service.port": 4149,
            "rustfs.service.port": 9199,
            "langfuse.service.port": 3439,
            "temporalDev.service.port": 7339,
            "workers.health.port": 8099,
            "workers.temporalNamespace": "tenant-one",
            "workers.taskQueue": "queue-one",
        },
    )
    assert lifecycle(documents)["data"]["temporalProfile"] == "kind"
    assert lifecycle(documents)["data"]["workersEnabled"] == "true"
    for component, port in (
        ("unified-api", 8189),
        ("frontend", 3339),
        ("litellm", 4149),
        ("rustfs", 9199),
        ("langfuse", 3439),
        ("temporal-dev", 7339),
    ):
        service = resource(documents, "Service", f"control-plane-{component}")
        deployment = resource(documents, "Deployment", f"control-plane-{component}")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert service["spec"]["ports"][0]["port"] == port
        assert any(entry["containerPort"] == port for entry in container["ports"])
    assert all(
        document["metadata"]["labels"]["app.kubernetes.io/instance"] == RELEASE
        for document in documents
        if document["kind"] == "Deployment"
    )
    litellm = resource(documents, "Deployment", "control-plane-litellm")["spec"][
        "template"
    ]["spec"]["containers"][0]
    assert litellm["args"][litellm["args"].index("--port") + 1] == "4149"
    rustfs = resource(documents, "Deployment", "control-plane-rustfs")["spec"][
        "template"
    ]["spec"]["containers"][0]
    assert environment(rustfs)["RUSTFS_ADDRESS"] == ":9199"
    langfuse = resource(documents, "Deployment", "control-plane-langfuse")["spec"][
        "template"
    ]["spec"]["containers"][0]
    assert environment(langfuse)["PORT"] == "3439"
    assert (
        environment(langfuse)["NEXTAUTH_URL"]
        == "http://control-plane-langfuse.lab-two.svc.cluster.local:3439"
    )
    api = resource(documents, "Deployment", "control-plane-unified-api")["spec"][
        "template"
    ]["spec"]["containers"][0]
    assert environment(api)["UVICORN_PORT"] == "8189"
    assert (
        environment(api)["LITELLM_URL"]
        == "http://control-plane-litellm.lab-two.svc.cluster.local:4149"
    )
    assert environment(api)["ADMIN_ALLOWLIST_PATH"] == "/etc/unified-api/allowlist.yaml"
    frontend = resource(documents, "Deployment", "control-plane-frontend")["spec"][
        "template"
    ]["spec"]["containers"][0]
    assert environment(frontend)["PORT"] == "3339"
    assert "NEXT_PUBLIC_API_URL" not in environment(frontend)
    worker = resource(documents, "Deployment", "control-plane-workers")["spec"][
        "template"
    ]["spec"]["containers"][0]
    assert (
        environment(worker)["UNIFIED_API_URL"]
        == "http://control-plane-unified-api.lab-two.svc.cluster.local:8189"
    )
    assert (
        environment(worker)["TEMPORAL_URL"]
        == "control-plane-temporal-dev.lab-two.svc.cluster.local:7339"
    )
    assert environment(worker)["TEMPORAL_NAMESPACE"] == "tenant-one"
    assert environment(worker)["TEMPORAL_TASK_QUEUE"] == "queue-one"
    assert environment(worker)["WORKER_HEALTH_PORT"] == "8099"
    worker_policy = resource(documents, "NetworkPolicy", "control-plane-workers-netpol")
    assert {7339, 8189, 4149} <= {
        entry["port"]
        for rule in worker_policy["spec"]["egress"]
        for entry in rule.get("ports", EMPTY_ITEMS)
    }
    cli = resource(documents, "Deployment", "control-plane-temporal-dev")["spec"][
        "template"
    ]["spec"]["containers"][0]
    assert (
        cli["image"]
        == "temporalio/admin-tools:1.32.0@sha256:a9f84fb9a374b2374fe2e67c8efc0468ff3f1c66c8a0b14597ec86e349e62bca"
    )
    assert cli["args"][cli["args"].index("--port") + 1] == "7339"
    assert cli["args"][cli["args"].index("--db-filename") + 1] == "/scratch/temporal.db"
    assert cli["args"][cli["args"].index("--namespace") + 1] == "tenant-one"
    pod = resource(documents, "Deployment", "control-plane-temporal-dev")["spec"][
        "template"
    ]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 1000
    assert pod["securityContext"]["runAsGroup"] == 1000
    assert pod["securityContext"]["fsGroup"] == 1000
    assert cli["securityContext"]["readOnlyRootFilesystem"] is True
    assert cli["securityContext"]["allowPrivilegeEscalation"] is False
    assert cli["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert cli["resources"] == {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"cpu": "1000m", "memory": "1Gi"},
    }
    assert environment(cli)["HOME"] == "/scratch"
    assert environment(cli)["TMPDIR"] == "/scratch"
    assert {
        volume["name"]: volume["emptyDir"]["sizeLimit"] for volume in pod["volumes"]
    } == {"scratch": "1Gi", "tmp": "128Mi"}
    assert {mount["name"]: mount["mountPath"] for mount in cli["volumeMounts"]} == {
        "scratch": "/scratch",
        "tmp": "/tmp",
    }
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert cli[probe]["exec"]["command"] == [
            "temporal",
            "--address",
            "127.0.0.1:7339",
            "operator",
            "cluster",
            "health",
        ]


def test_full_temporal_uses_actual_v1_sql_projection(chart_dir: str) -> None:
    chart = Path(chart_dir)
    overrides = {
        "fullnameOverride": "control-plane",
        "temporal.server.frontend.service.port": 7338,
    }
    bootstrap = render(chart, {**overrides, "bootstrap.enabled": True})
    fragment = lifecycle(bootstrap)["data"]["temporal-values.yaml"]
    documents = render(
        chart,
        {
            **overrides,
            "temporal.enabled": True,
            "bootstrap.prerequisitesReady": True,
            "workers.enabled": True,
        },
        values_yaml=fragment,
        upgrade=True,
    )
    assert lifecycle(documents)["data"]["temporalProfile"] == "full"
    assert lifecycle(documents)["data"]["workersEnabled"] == "true"
    config = resource(documents, "ConfigMap", "raven-temporal-config")
    rendered_config = config["data"]["config_template.yaml"]
    for expected in (
        "defaultStore: default",
        "visibilityStore: visibility",
        "pluginName: postgres12",
        "connectAddr: raven-db.lab-two.svc.cluster.local:5432",
        "databaseName: temporal",
        "databaseName: temporal_visibility",
        "serverName: raven-db.lab-two.svc.cluster.local",
        "caFile: /etc/db-trust/ca.crt",
    ):
        assert expected in rendered_config
    assert "existingSecret:" not in rendered_config
    assert "driver: sql" not in rendered_config
    persistence = yaml.safe_load(
        re.sub(r"\{\{[^{}]*\}\}", '"injected"', rendered_config)
    )["persistence"]
    assert persistence["defaultStore"] == "default"
    assert persistence["visibilityStore"] == "visibility"
    for store, database in (
        ("default", "temporal"),
        ("visibility", "temporal_visibility"),
    ):
        sql = persistence["datastores"][store]["sql"]
        assert sql["pluginName"] == "postgres12"
        assert sql["databaseName"] == database
        assert sql["connectAddr"] == "raven-db.lab-two.svc.cluster.local:5432"
        assert sql["user"] == "temporal"
        assert sql["tls"] == {
            "enabled": True,
            "caFile": "/etc/db-trust/ca.crt",
            "enableHostVerification": True,
            "serverName": "raven-db.lab-two.svc.cluster.local",
        }
        assert (
            "existingSecret" not in sql
            and "createDatabase" not in sql
            and "manageSchema" not in sql
        )
    frontend_pod = resource(documents, "Deployment", "raven-temporal-frontend")["spec"][
        "template"
    ]["spec"]
    frontend = frontend_pod["containers"][0]
    for variable in (
        "TEMPORAL_DEFAULT_STORE_PASSWORD",
        "TEMPORAL_VISIBILITY_STORE_PASSWORD",
    ):
        assert environment(frontend)[variable]["secretKeyRef"] == {
            "name": "temporal.raven-db.credentials.postgresql.acid.zalan.do",
            "key": "password",
        }
    assert frontend["securityContext"]["readOnlyRootFilesystem"] is True
    server_mounts = {mount["name"]: mount for mount in frontend["volumeMounts"]}
    assert (
        server_mounts["db-trust"]["mountPath"] == "/etc/db-trust"
        and server_mounts["db-trust"]["readOnly"] is True
    )
    assert server_mounts["server-runtime-config"]["mountPath"] == "/etc/temporal/config"
    assert (
        next(
            volume
            for volume in frontend_pod["volumes"]
            if volume["name"] == "server-runtime-config"
        )["emptyDir"]["sizeLimit"]
        == "128Mi"
    )
    assert (
        next(
            volume for volume in frontend_pod["volumes"] if volume["name"] == "db-trust"
        )["configMap"]["name"]
        == "raven-db-trust"
    )
    job = next(
        document
        for document in documents
        if document["kind"] == "Job"
        and "pre-upgrade"
        in document["metadata"].get("annotations", EMPTY_MAP).get("helm.sh/hook", "")
    )
    assert "pre-upgrade" in job["metadata"]["annotations"]["helm.sh/hook"]
    assert (
        job["spec"]["template"]["spec"]["serviceAccountName"]
        == "control-plane-temporal-schema"
    )
    init_containers = job["spec"]["template"]["spec"]["initContainers"]
    assert {container["name"] for container in init_containers} == {
        "manage-schema-default-store",
        "manage-schema-visibility-store",
    }
    for container in init_containers:
        env = environment(container)
        assert (
            env["SQL_DATABASE"]
            == {
                "manage-schema-default-store": "temporal",
                "manage-schema-visibility-store": "temporal_visibility",
            }[container["name"]]
        )
        assert env["SQL_USER"] == "temporal"
        assert env["SQL_TLS"] == "true"
        assert env["SQL_TLS_CA_FILE"] == "/etc/db-trust/ca.crt"
        assert env["SQL_PLUGIN"] == "postgres12"
        assert env["SQL_HOST"] == "raven-db.lab-two.svc.cluster.local"
        assert env["SQL_PORT"] == "5432"
        assert (
            env["SQL_PASSWORD"]["secretKeyRef"]["name"]
            == "temporal.raven-db.credentials.postgresql.acid.zalan.do"
        )
        assert env["SQL_TLS_SERVER_NAME"] == "raven-db.lab-two.svc.cluster.local"
        assert env["SQL_TLS_DISABLE_HOST_VERIFICATION"] == "false"
        assert any(
            mount["name"] == "db-trust" and mount["readOnly"]
            for mount in container["volumeMounts"]
        )
    volumes = job["spec"]["template"]["spec"]["volumes"]
    assert next(volume for volume in volumes if volume["name"] == "db-trust")[
        "configMap"
    ] == {"name": "raven-db-trust", "items": [{"key": "ca.crt", "path": "ca.crt"}]}
    worker = resource(documents, "Deployment", "control-plane-workers")["spec"][
        "template"
    ]["spec"]["containers"][0]
    assert (
        environment(worker)["TEMPORAL_URL"]
        == "raven-temporal-frontend.lab-two.svc.cluster.local:7338"
    )
    assert (
        resource(documents, "Service", "raven-temporal-frontend")["spec"]["ports"][0][
            "port"
        ]
        == 7338
    )
    namespace_job = next(
        document
        for document in documents
        if document["kind"] == "Job"
        and "post-upgrade"
        in document["metadata"].get("annotations", EMPTY_MAP).get("helm.sh/hook", "")
    )
    assert (
        namespace_job["spec"]["template"]["spec"]["initContainers"][0]["name"]
        == "create-default-namespace"
    )
    server_only = render(
        chart,
        {
            **overrides,
            "temporal.enabled": True,
            "bootstrap.prerequisitesReady": True,
            "workers.enabled": False,
        },
        values_yaml=fragment,
        upgrade=True,
    )
    assert not any(
        document["kind"] == "Deployment"
        and document["metadata"]["name"] == "control-plane-workers"
        for document in server_only
    )
    assert any(
        document["kind"] == "Job"
        and "post-upgrade"
        in document["metadata"].get("annotations", EMPTY_MAP).get("helm.sh/hook", "")
        for document in server_only
    )
    # Re-projecting an already composed full release must not duplicate reserved volumes.
    assert lifecycle(documents)["data"]["temporal-values.yaml"] == fragment


@pytest.mark.parametrize("component", ["server", "admintools"])
def test_projection_preserves_extra_volumes(chart_dir: str, component: str) -> None:
    volume = {"name": "extra", "emptyDir": {"sizeLimit": "64Mi"}}
    mount = {"name": "extra", "mountPath": "/extra"}
    inputs = yaml.safe_dump(
        {
            "temporal": {
                component: {
                    "additionalVolumes": [volume],
                    "additionalVolumeMounts": [mount],
                }
            }
        }
    )
    bootstrap = render(Path(chart_dir), {"bootstrap.enabled": True}, values_yaml=inputs)
    fragment = lifecycle(bootstrap)["data"]["temporal-values.yaml"]
    projected = yaml.safe_load(fragment)
    assert volume in projected["temporal"][component]["additionalVolumes"]
    assert mount in projected["temporal"][component]["additionalVolumeMounts"]
    full = render(
        Path(chart_dir),
        {"temporal.enabled": True, "bootstrap.prerequisitesReady": True},
        values_yaml=fragment,
        upgrade=True,
    )
    consumer = (
        resource(full, "Deployment", "raven-temporal-frontend")
        if component == "server"
        else resource(full, "Job", "raven-temporal-schema")
    )
    pod = consumer["spec"]["template"]["spec"]
    assert volume in pod["volumes"]
    containers = pod["containers"] if component == "server" else pod["initContainers"]
    assert all(mount in container["volumeMounts"] for container in containers)


@pytest.mark.parametrize(
    "settings,error",
    [
        ({"temporal.enabled": True, "temporalDev.enabled": True}, "mutually exclusive"),
        (
            {"bootstrap.enabled": True, "temporalDev.enabled": True},
            "bootstrap.enabled requires",
        ),
        ({"workers.enabled": True}, "requires one Temporal profile"),
        ({"temporal.enabled": True}, "prerequisitesReady=true"),
        (
            {"temporal.enabled": True, "bootstrap.prerequisitesReady": True},
            "cannot be the first install",
        ),
        ({"frontend.service.port": 70000}, "between 1 and 65535"),
        ({"workers.health.port": 0}, "between 1 and 65535"),
        ({"workers.health.port": 70000}, "between 1 and 65535"),
        ({"temporal.server.frontend.service.port": 70000}, "between 1 and 65535"),
        ({"database.port": 5544}, "Spilo listener"),
        ({"database.substrateUser": "postgres"}, "distinct non-system SQL role"),
        ({"database.substrateDatabase": "postgres"}, "distinct application database"),
    ],
)
def test_invalid_profiles_fail(
    chart_dir: str, settings: dict[str, Any], error: str
) -> None:
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(Path(chart_dir), settings)
    assert error in captured.value.stderr


def test_projection_rejects_duplicate_or_reserved_volume_conflicts(
    chart_dir: str,
) -> None:
    for volumes in (
        [
            {"name": "extra", "emptyDir": EMPTY_MAP},
            {"name": "extra", "emptyDir": EMPTY_MAP},
        ],
        [{"name": "scratch", "emptyDir": {"sizeLimit": "2Gi"}}],
    ):
        inputs = yaml.safe_dump(
            {"temporal": {"server": {"additionalVolumes": volumes}}}
        )
        with pytest.raises(subprocess.CalledProcessError) as captured:
            render(Path(chart_dir), {"bootstrap.enabled": True}, values_yaml=inputs)
        assert (
            "duplicated" in captured.value.stderr
            or "conflicts" in captured.value.stderr
        )


@pytest.mark.parametrize(
    "component,name",
    [
        ("server", "config"),
        ("server", "dynamic-config"),
        ("admintools", "shims"),
        ("admintools", "client-tls"),
    ],
)
def test_projection_rejects_dependency_volume_names(
    chart_dir: str, component: str, name: str
) -> None:
    inputs = yaml.safe_dump(
        {
            "temporal": {
                component: {
                    "additionalVolumes": [
                        {"name": name, "emptyDir": {"sizeLimit": "64Mi"}}
                    ]
                }
            }
        }
    )
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(Path(chart_dir), {"bootstrap.enabled": True}, values_yaml=inputs)
    assert "collides with the pinned dependency" in captured.value.stderr


@pytest.mark.parametrize(
    "component,path",
    [
        ("server", "/tmp"),
        ("server", "/etc/temporal/config/config_template.yaml"),
        ("server", "/etc/temporal/dynamic_config"),
        ("admintools", "/etc/db-trust"),
        ("admintools", "/etc/temporal/tls/client"),
    ],
)
def test_projection_rejects_conflicting_mount_paths(
    chart_dir: str, component: str, path: str
) -> None:
    inputs = yaml.safe_dump(
        {
            "temporal": {
                component: {
                    "additionalVolumeMounts": [{"name": "extra", "mountPath": path}]
                }
            }
        }
    )
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(Path(chart_dir), {"bootstrap.enabled": True}, values_yaml=inputs)
    assert any(
        reason in captured.value.stderr
        for reason in ("conflicts", "collides", "shadows")
    )


def test_full_without_projection_rejected_on_upgrade(chart_dir: str) -> None:
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(
            Path(chart_dir),
            {
                "temporal.enabled": True,
                "bootstrap.prerequisitesReady": True,
                "temporal.schema.useHelmHooks": True,
                "temporal.server.config.namespaces.create": True,
                "temporal.server.config.namespaces.useHelmHooks": True,
                "temporal.server.config.namespaces.namespace[0].name": "default",
            },
            upgrade=True,
        )
    assert (
        "connectAddr" in captured.value.stderr
        or "datastore projection" in captured.value.stderr
    )


def test_substrate_service_tls_database_and_policy_alignment(chart_dir: str) -> None:
    substrate = Path(chart_dir).parent / "agent-substrate"
    documents = render(
        substrate,
        {
            "ateapi.service.port": 51159,
            "platformNamespace": "backing",
            "database.host": "raven-db",
            "database.user": "sandbox_role",
            "database.name": "sandbox_state",
            "database.port": 5549,
            "database.trustConfigMap": "raven-db-trust",
            "rustfs.host": "raven-platform-rustfs",
            "rustfs.port": 9195,
            "global.apiServerCIDRs[0]": "10.20.0.1/32",
        },
    )
    service_name = "raven-agent-substrate-ateapi"
    service = resource(documents, "Service", service_name)
    assert service["spec"]["ports"][0] == {
        "port": 51159,
        "targetPort": "grpc",
        "name": "grpc",
    }
    api = resource(documents, "Deployment", service_name)["spec"]["template"]["spec"]
    container = api["containers"][0]
    assert "--grpc-listen-addr=0.0.0.0:51159" in container["args"]
    assert "--grpc-server-cred-bundle=/etc/tls/bundle.pem" in container["args"]
    assert "--pod-identity-ca-certs=/etc/tls/ca.crt" in container["args"]
    assert container["ports"][0]["containerPort"] == 51159
    assert container["readinessProbe"]["tcpSocket"]["port"] == 51159
    assert (
        environment(container)["PGUSER"]["secretKeyRef"]["name"]
        == "sandbox-role.raven-db.credentials.postgresql.acid.zalan.do"
    )
    assert environment(container)["PGPASSWORD"] == {
        "secretKeyRef": {
            "name": "sandbox-role.raven-db.credentials.postgresql.acid.zalan.do",
            "key": "password",
        }
    }
    assert environment(container)["POSTGRES_URL"] == (
        "postgres://$(PGUSER)@raven-db.backing.svc.cluster.local:5549/sandbox_state"
        "?sslmode=verify-full&sslrootcert=/etc/db-trust/ca.crt&connect_timeout=10"
    )
    assert "$(PGPASSWORD)" not in " ".join(container["args"])
    assert "--postgres-connection-string=$(POSTGRES_URL)" in container["args"]
    assert (
        environment(container)["AWS_ENDPOINT_URL_S3"]
        == "http://raven-platform-rustfs.backing.svc.cluster.local:9195"
    )
    policy = resource(documents, "NetworkPolicy", service_name)["spec"]
    assert policy["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 51159}]
    assert (
        policy["ingress"][0]["from"][0]["podSelector"]["matchLabels"][
            "app.kubernetes.io/instance"
        ]
        == RELEASE
    )
    database_rule = next(
        rule
        for rule in policy["egress"]
        if any(
            peer.get("podSelector", EMPTY_MAP)
            .get("matchLabels", EMPTY_MAP)
            .get("cluster-name")
            == "raven-db"
            for peer in rule.get("to", EMPTY_ITEMS)
        )
    )
    assert database_rule["ports"] == [{"protocol": "TCP", "port": 5549}]
    assert database_rule["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "backing"
    }
    storage_rule = next(
        rule
        for rule in policy["egress"]
        if any(
            peer.get("podSelector", EMPTY_MAP).get("matchLabels", EMPTY_MAP).get("app")
            == "rustfs"
            for peer in rule.get("to", EMPTY_ITEMS)
        )
    )
    assert storage_rule["ports"] == [{"protocol": "TCP", "port": 9195}]
    api_server_rule = next(
        rule
        for rule in policy["egress"]
        if any("ipBlock" in peer for peer in rule.get("to", EMPTY_ITEMS))
    )
    assert api_server_rule["to"] == [{"ipBlock": {"cidr": "10.20.0.1/32"}}]
    assert api_server_rule["ports"] == [
        {"protocol": "TCP", "port": 443},
        {"protocol": "TCP", "port": 6443},
    ]
    controller_policy = resource(
        documents, "NetworkPolicy", "raven-agent-substrate-atecontroller"
    )["spec"]
    api_rule = next(
        rule
        for rule in controller_policy["egress"]
        if any(
            peer.get("podSelector", EMPTY_MAP)
            .get("matchLabels", EMPTY_MAP)
            .get("app.kubernetes.io/component")
            == "ateapi"
            for peer in rule.get("to", EMPTY_ITEMS)
        )
    )
    assert api_rule["ports"] == [{"protocol": "TCP", "port": 51159}]
    trust = next(volume for volume in api["volumes"] if volume["name"] == "db-trust")
    assert trust["configMap"]["name"] == "raven-db-trust"
    assert "secret" not in trust
    tls = resource(documents, "Secret", "raven-agent-substrate-tls")
    certificate = base64.b64decode(tls["data"]["tls.crt"])
    parsed = x509.load_pem_x509_certificate(certificate)
    names = parsed.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)
    assert "raven-agent-substrate-ateapi.lab-two.svc.cluster.local" in names
    controller = resource(
        documents, "Deployment", "raven-agent-substrate-atecontroller"
    )["spec"]["template"]["spec"]["containers"][0]
    assert (
        "--ateapi-server-name=raven-agent-substrate-ateapi.lab-two.svc.cluster.local"
        in controller["args"]
    )
    assert (
        "--ateapi-conn-spec=raven-agent-substrate-ateapi.lab-two.svc.cluster.local:51159"
        in controller["args"]
    )
    role = resource(documents, "Role", "raven-agent-substrate-egress-secret-reader")
    assert role["metadata"]["namespace"] == NAMESPACE
    assert role["rules"][0]["resourceNames"] == ["egress-mitm-ca-pool"]
    assert all(
        "secrets" not in rule["resources"]
        for document in documents
        if document["kind"] == "ClusterRole"
        for rule in document["rules"]
    )


def assert_substrate_bundle_matches_pinned_loader(secret: Manifest) -> None:
    """v0.3.0 credbundle.Parse rejects RSA/EC PRIVATE KEY PEM blocks."""
    data = {name: base64.b64decode(value) for name, value in secret["data"].items()}
    bundle = data["bundle.pem"]
    assert re.findall(rb"-----BEGIN ([A-Z ]+)-----", bundle) == [
        b"PRIVATE KEY",
        b"CERTIFICATE",
        b"CERTIFICATE",
    ]
    assert bundle == b"\n".join([data["tls.key"], data["tls.crt"], data["ca.crt"]])
    key = serialization.load_pem_private_key(data["tls.key"], password=None)
    assert (
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        == data["tls.key"]
    )
    leaf, authority = x509.load_pem_x509_certificates(bundle)
    assert leaf.public_bytes(serialization.Encoding.PEM) == data["tls.crt"]
    assert authority.public_bytes(serialization.Encoding.PEM) == data["ca.crt"]
    assert key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) == leaf.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    leaf.verify_directly_issued_by(authority)
    purposes = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in purposes
    assert x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH in purposes
    names = leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)
    assert set(names) == {
        "raven-agent-substrate-ateapi",
        "raven-agent-substrate-ateapi.lab-two.svc",
        "raven-agent-substrate-ateapi.lab-two.svc.cluster.local",
    }


def substrate_tls_checksums(documents: list[Manifest]) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for component in ("ateapi", "atecontroller"):
        metadata = resource(
            documents, "Deployment", f"raven-agent-substrate-{component}"
        )["spec"]["template"]["metadata"]
        checksums[component] = metadata.get("annotations", {}).get("checksum/tls", "")
    return checksums


def assert_substrate_tls_checksums_match_secret(documents: list[Manifest]) -> None:
    secret = resource(documents, "Secret", "raven-agent-substrate-tls")
    data = json.dumps(secret["data"], sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(data.encode()).hexdigest()
    assert substrate_tls_checksums(documents) == {
        "ateapi": expected,
        "atecontroller": expected,
    }


@pytest.mark.parametrize("secret_template", ["secret-tls.yaml", "000-secret-tls.yaml"])
def test_substrate_bundle_matches_pinned_pkcs8_loader(
    chart_dir: str, tmp_path: Path, secret_template: str
) -> None:
    chart = tmp_path / "agent-substrate"
    shutil.copytree(Path(chart_dir).parent / "agent-substrate", chart)
    # Exercise both Secret-first and Deployment-first native rendering order.
    if secret_template != "secret-tls.yaml":
        (chart / "templates" / "secret-tls.yaml").rename(
            chart / "templates" / secret_template
        )
    documents = render(chart)
    assert_substrate_bundle_matches_pinned_loader(
        resource(documents, "Secret", "raven-agent-substrate-tls")
    )
    assert_substrate_tls_checksums_match_secret(documents)


@pytest.mark.parametrize("key_type", ["ed25519", "rsa", "ecdsa", "malformed"])
def test_substrate_tls_lookup_preserves_compatible_keys_and_rotates_legacy(
    chart_dir: str, tmp_path: Path, key_type: str
) -> None:
    chart = tmp_path / "agent-substrate"
    shutil.copytree(Path(chart_dir).parent / "agent-substrate", chart)
    template = chart / "templates" / "_helpers.tpl"
    source = template.read_text()
    if key_type in {"rsa", "ecdsa"}:
        # Produce matching legacy key/certificate pairs with native Helm crypto.
        template.write_text(
            source.replace('genPrivateKey "ed25519"', f'genPrivateKey "{key_type}"')
        )
    original_documents = render(chart)
    assert_substrate_tls_checksums_match_secret(original_documents)
    original = resource(original_documents, "Secret", "raven-agent-substrate-tls")
    if key_type == "malformed":
        original["data"]["tls.key"] = base64.b64encode(b"invalid-key").decode()
    # Offline Helm lookup has no cluster. Stub only that external read, leaving
    # issuance, migration and deployment checksum templates unchanged.
    lookup = 'lookup "v1" "Secret" .Release.Namespace $secretName'
    assert source.count(lookup) == 1
    existing = json.dumps(json.dumps({"data": original["data"]}))
    template.write_text(source.replace(lookup, f"(fromJson {existing})"))
    migrated = render(chart, upgrade=True)
    replacement = resource(migrated, "Secret", "raven-agent-substrate-tls")
    assert_substrate_bundle_matches_pinned_loader(replacement)
    assert_substrate_tls_checksums_match_secret(migrated)
    retained_lookup = json.dumps(json.dumps({"data": replacement["data"]}))
    template.write_text(source.replace(lookup, f"(fromJson {retained_lookup})"))
    retained = render(chart, upgrade=True)
    assert (
        resource(retained, "Secret", "raven-agent-substrate-tls")["data"]
        == replacement["data"]
    )
    assert_substrate_tls_checksums_match_secret(retained)
    assert substrate_tls_checksums(retained) == substrate_tls_checksums(migrated)
    if key_type == "ed25519":
        assert replacement["data"] == original["data"]
        checksums = substrate_tls_checksums(migrated)
        assert checksums == substrate_tls_checksums(original_documents)
        assert checksums["ateapi"] == checksums["atecontroller"]
        assert substrate_tls_checksums(render(chart, upgrade=True)) == checksums
        # A changed chart-owned key/CA must affect both consumers' pod templates.
        changed = resource(
            render(Path(chart_dir).parent / "agent-substrate"),
            "Secret",
            "raven-agent-substrate-tls",
        )
        changed_lookup = json.dumps(json.dumps({"data": changed["data"]}))
        template.write_text(source.replace(lookup, f"(fromJson {changed_lookup})"))
        updated = substrate_tls_checksums(render(chart, upgrade=True))
        assert all(
            updated[component] != checksums[component] for component in checksums
        )
    else:
        for key in ("tls.key", "tls.crt", "ca.crt"):
            assert replacement["data"][key] != original["data"][key]
    if key_type == "malformed":
        original["data"]["tls.key"] = base64.b64encode(
            b"-----BEGIN " + b"PRIVATE KEY-----\nYWJj\n-----END PRIVATE KEY-----\n"
        ).decode()
        broken_lookup = json.dumps(json.dumps({"data": original["data"]}))
        template.write_text(source.replace(lookup, f"(fromJson {broken_lookup})"))
        with pytest.raises(subprocess.CalledProcessError) as captured:
            render(chart, upgrade=True)
        assert "decoding PEM as PKCS#8" in captured.value.stderr
    external = render(
        chart, {"tls.existingBundleSecret": "owner-managed-bundle"}, upgrade=True
    )
    assert not any(document["kind"] == "Secret" for document in external)
    assert not any(substrate_tls_checksums(external).values())


@pytest.mark.parametrize(
    "field,port",
    [
        ("ateapi.service.port", 0),
        ("ateapi.service.port", 70000),
        ("database.port", 0),
        ("database.port", 70000),
        ("rustfs.port", 0),
        ("rustfs.port", 70000),
    ],
)
def test_invalid_substrate_ports_rejected(
    chart_dir: str, field: str, port: int
) -> None:
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(Path(chart_dir).parent / "agent-substrate", {field: port})
    assert "ports must be between 1 and 65535" in captured.value.stderr


def test_full_long_release_namespace_address_matches_service(chart_dir: str) -> None:
    release = "z" * 50
    bootstrap = render(Path(chart_dir), {"bootstrap.enabled": True}, release=release)
    fragment = lifecycle(bootstrap)["data"]["temporal-values.yaml"]
    full = render(
        Path(chart_dir),
        {"temporal.enabled": True, "bootstrap.prerequisitesReady": True},
        values_yaml=fragment,
        upgrade=True,
        release=release,
    )
    expected_service = "z" * 50 + "-te-frontend"
    resource(full, "Service", expected_service)
    namespace_job = next(
        document
        for document in full
        if document["kind"] == "Job"
        and "post-upgrade"
        in document["metadata"].get("annotations", EMPTY_MAP).get("helm.sh/hook", "")
    )
    assert len(namespace_job["metadata"]["name"]) <= 63
    assert (
        environment(namespace_job["spec"]["template"]["spec"]["initContainers"][0])[
            "TEMPORAL_ADDRESS"
        ]
        == expected_service + ":7233"
    )
    assert lifecycle(full)["data"]["temporalService"] == expected_service


def test_full_temporal_long_name_label_normalization(chart_dir: str) -> None:
    overrides = {
        "temporal.nameOverride": "a" * 64 + "-",
        "temporal.fullnameOverride": "fixed-temporal",
        "bootstrap.enabled": True,
    }
    bootstrap = render(Path(chart_dir), overrides)
    fragment = lifecycle(bootstrap)["data"]["temporal-values.yaml"]
    full = render(
        Path(chart_dir),
        {
            **overrides,
            "bootstrap.enabled": False,
            "temporal.enabled": True,
            "bootstrap.prerequisitesReady": True,
            "workers.enabled": True,
        },
        values_yaml=fragment,
        upgrade=True,
    )
    frontend = resource(full, "Deployment", "fixed-temporal-frontend")
    assert (
        frontend["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"]
        == "a" * 63
    )
    policy = resource(full, "NetworkPolicy", "raven-platform-workers-netpol")["spec"]
    peer = next(
        peer
        for rule in policy["egress"]
        for peer in rule.get("to", EMPTY_ITEMS)
        if peer.get("podSelector", EMPTY_MAP)
        .get("matchLabels", EMPTY_MAP)
        .get("app.kubernetes.io/component")
        == "frontend"
    )
    assert peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "a" * 63


@pytest.mark.parametrize(
    "field,value",
    [
        ("server.config.persistence.datastores.default.sql.tls.enabled", False),
        ("server.config.persistence.datastores.visibility.sql.databaseName", "wrong"),
        ("server.config.persistence.datastores.default.sql.existingSecret", "wrong"),
        ("server.config.persistence.datastores.default.sql.manageSchema", False),
    ],
)
def test_full_owned_sql_contract_cannot_be_overridden(
    chart_dir: str, field: str, value: str | bool
) -> None:
    bootstrap = render(Path(chart_dir), {"bootstrap.enabled": True})
    fragment = lifecycle(bootstrap)["data"]["temporal-values.yaml"]
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(
            Path(chart_dir),
            {
                "temporal.enabled": True,
                "bootstrap.prerequisitesReady": True,
                f"temporal.{field}": value,
            },
            values_yaml=fragment,
            upgrade=True,
        )
    assert "datastore projection" in captured.value.stderr


def test_full_rejects_cli_overrides_that_remove_required_mounts(chart_dir: str) -> None:
    bootstrap = render(Path(chart_dir), {"bootstrap.enabled": True})
    fragment = lifecycle(bootstrap)["data"]["temporal-values.yaml"]
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(
            Path(chart_dir),
            {
                "temporal.enabled": True,
                "bootstrap.prerequisitesReady": True,
                "temporal.server.additionalVolumes[0].name": "extra",
            },
            values_yaml=fragment,
            upgrade=True,
        )
    assert "volume/mount projection" in captured.value.stderr


def test_projection_preserves_yaml_sensitive_temporal_fullname(chart_dir: str) -> None:
    inputs = yaml.safe_dump({"temporal": {"fullnameOverride": "null"}})
    bootstrap = render(Path(chart_dir), {"bootstrap.enabled": True}, values_yaml=inputs)
    fragment = lifecycle(bootstrap)["data"]["temporal-values.yaml"]
    assert yaml.safe_load(fragment)["temporal"]["fullnameOverride"] == "null"
    full = render(
        Path(chart_dir),
        {"temporal.enabled": True, "bootstrap.prerequisitesReady": True},
        values_yaml=fragment,
        upgrade=True,
    )
    resource(full, "Service", "null-frontend")
    namespace_job = next(
        document
        for document in full
        if document["kind"] == "Job"
        and "post-upgrade"
        in document["metadata"].get("annotations", EMPTY_MAP).get("helm.sh/hook", "")
    )
    assert (
        environment(namespace_job["spec"]["template"]["spec"]["initContainers"][0])[
            "TEMPORAL_ADDRESS"
        ]
        == "null-frontend:7233"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema.useHelmHooks", False),
        ("server.config.namespaces.create", False),
        ("server.config.namespaces.useHelmHooks", False),
        ("server.config.namespaces.namespace[0].name", "other"),
    ],
)
def test_full_namespace_and_hook_contracts_cannot_be_overridden(
    chart_dir: str, field: str, value: str | bool
) -> None:
    bootstrap = render(
        Path(chart_dir),
        {"bootstrap.enabled": True, "workers.temporalNamespace": "tenant-one"},
    )
    fragment = lifecycle(bootstrap)["data"]["temporal-values.yaml"]
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(
            Path(chart_dir),
            {
                "temporal.enabled": True,
                "bootstrap.prerequisitesReady": True,
                "workers.temporalNamespace": "tenant-one",
                f"temporal.{field}": value,
            },
            values_yaml=fragment,
            upgrade=True,
        )
    assert (
        "namespace" in captured.value.stderr or "schema hooks" in captured.value.stderr
    )


@pytest.mark.parametrize(
    "component,path",
    [
        ("server", "/etc/db-trust/ca.crt"),
        ("server", "/etc/db-trust/"),
        ("server", "/etc/db-trust/../db-trust/ca.crt"),
        ("server", "/etc"),
        ("server", "/etc/temporal/config/docker.yaml"),
        ("admintools", "/etc/db-trust/ca.crt"),
    ],
)
def test_additional_mounts_cannot_shadow_owned_trust_or_config(
    chart_dir: str, component: str, path: str
) -> None:
    inputs = yaml.safe_dump(
        {
            "temporal": {
                component: {
                    "additionalVolumes": [
                        {"name": "shadow", "emptyDir": {"sizeLimit": "1Mi"}}
                    ],
                    "additionalVolumeMounts": [{"name": "shadow", "mountPath": path}],
                }
            }
        }
    )
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(Path(chart_dir), {"bootstrap.enabled": True}, values_yaml=inputs)
    assert "shadows" in captured.value.stderr or "normalized" in captured.value.stderr
    clean = render(Path(chart_dir), {"bootstrap.enabled": True})
    projected = yaml.safe_load(lifecycle(clean)["data"]["temporal-values.yaml"])
    projected["temporal"][component]["additionalVolumes"].append(
        {"name": "shadow", "emptyDir": {"sizeLimit": "1Mi"}}
    )
    projected["temporal"][component]["additionalVolumeMounts"].append(
        {"name": "shadow", "mountPath": path}
    )
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(
            Path(chart_dir),
            {"temporal.enabled": True, "bootstrap.prerequisitesReady": True},
            values_yaml=yaml.safe_dump(projected),
            upgrade=True,
        )
    assert "shadows" in captured.value.stderr or "normalized" in captured.value.stderr


def test_ocp_substrate_requires_explicit_assembled_bundle(chart_dir: str) -> None:
    substrate = Path(chart_dir).parent / "agent-substrate"
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(substrate, {"global.tls.useOcpServingCert": True})
    assert "requires tls.existingBundleSecret" in captured.value.stderr
    documents = render(
        substrate,
        {
            "global.tls.useOcpServingCert": True,
            "tls.existingBundleSecret": "prepared-api-bundle",
        },
    )
    assert not any(document["kind"] == "Secret" for document in documents)
    assert not any(substrate_tls_checksums(documents).values())
    api = resource(documents, "Deployment", "raven-agent-substrate-ateapi")["spec"][
        "template"
    ]["spec"]
    secret = next(
        volume["secret"] for volume in api["volumes"] if volume["name"] == "tls"
    )
    assert secret["secretName"] == "prepared-api-bundle"
    assert {item["key"] for item in secret["items"]} == {
        "bundle.pem",
        "ca.crt",
        "auth.yaml",
        "empty.json",
    }
    controller = resource(
        documents, "Deployment", "raven-agent-substrate-atecontroller"
    )["spec"]["template"]["spec"]
    secret = next(
        volume["secret"] for volume in controller["volumes"] if volume["name"] == "tls"
    )
    assert secret["secretName"] == "prepared-api-bundle"
    assert {item["key"] for item in secret["items"]} == {"bundle.pem", "ca.crt"}


def test_long_caddy_names_share_actual_pvc(chart_dir: str) -> None:
    documents = render(Path(chart_dir), {"fullnameOverride": "a" * 49})
    deployment = next(
        document
        for document in documents
        if document["kind"] == "Deployment"
        and document["metadata"]["labels"]["app"] == "caddy-tailscale"
    )
    claim = next(
        volume["persistentVolumeClaim"]["claimName"]
        for volume in deployment["spec"]["template"]["spec"]["volumes"]
        if "persistentVolumeClaim" in volume
    )
    resource(documents, "PersistentVolumeClaim", claim)
    assert len(claim) <= 63


def test_api_shared_config_survives_disabled_litellm(chart_dir: str) -> None:
    documents = render(Path(chart_dir), {"litellm.enabled": False})
    resource(documents, "Deployment", "raven-platform-unified-api")
    resource(documents, "ConfigMap", "raven-platform-litellm-config")
    assert not any(
        document["kind"] == "Deployment"
        and document["metadata"]["name"] == "raven-platform-litellm"
        for document in documents
    )


def test_substrate_metadata_matches_selected_source_version(chart_dir: str) -> None:
    metadata = yaml.safe_load(
        (Path(chart_dir).parent / "agent-substrate" / "Chart.yaml").read_text()
    )
    source = (
        Path(__file__).absolute().parents[2]
        / "images"
        / "agent-substrate"
        / "Dockerfile"
    ).read_text()
    selected = re.search(r"^ARG AGENT_SUBSTRATE_VERSION=(\S+)$", source, re.MULTILINE)
    assert selected is not None
    assert metadata["appVersion"] == selected.group(1).removeprefix("v")


def test_provider_pin_lock_and_packaged_crds(chart_dir: str) -> None:
    wrapper = Path(chart_dir).parent / "postgres-operator"
    metadata = yaml.safe_load((wrapper / "Chart.yaml").read_text())
    lock = yaml.safe_load((wrapper / "Chart.lock").read_text())
    dependency = metadata["dependencies"][0]
    assert dependency["name"] == "postgres-operator"
    assert str(dependency["version"]) == "2.0.3"
    assert lock["dependencies"] == metadata["dependencies"]
    archive = wrapper / "charts" / "postgres-operator-2.0.3.tgz"
    with tarfile.open(archive) as artifact:
        for name in ("postgresqls", "operatorconfigurations", "postgresteams"):
            source = artifact.extractfile(f"postgres-operator/crds/{name}.yaml")
            assert source is not None
            assert (wrapper / "crds" / f"{name}.yaml").read_bytes() == source.read()
    values = yaml.safe_load((wrapper / "values.yaml").read_text())
    assert (
        values["postgres-operator"]["configGeneral"]["enable_crd_registration"] is False
    )


def test_provider_spilo_identity_can_read_group_owned_tls(chart_dir: str) -> None:
    wrapper = Path(chart_dir).parent / "postgres-operator"
    configuration = resource(
        render(wrapper), "OperatorConfiguration", "raven-postgres-operator"
    )["configuration"]["kubernetes"]
    assert configuration["enable_readiness_probe"] is True
    assert configuration["spilo_runasuser"] == 101
    assert configuration["spilo_runasgroup"] == 103
    assert configuration["spilo_fsgroup"] == 103
