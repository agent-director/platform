"""Independent rendered contracts; these tests never deploy or run containers."""

import base64
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization

Manifest = dict[str, Any]
EMPTY_ITEMS: tuple[object, ...] = ()
RELEASE = "raven"
NAMESPACE = "lab-two"


def render(
    chart: Path,
    settings: Mapping[str, str | int | bool] | None = None,
) -> list[Manifest]:
    command = ["helm", "template", RELEASE, str(chart), "--namespace", NAMESPACE]
    for key, value in (settings or {}).items():
        serialized = str(value).lower() if isinstance(value, bool) else str(value)
        command.extend(["--set", f"{key}={serialized}"])
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def resource(documents: list[Manifest], kind: str, name: str) -> Manifest:
    return next(
        document
        for document in documents
        if document["kind"] == kind and document["metadata"]["name"] == name
    )


def environment(container: Manifest) -> dict[str, Any]:
    return {
        item["name"]: item.get("value", item.get("valueFrom"))
        for item in container.get("env", EMPTY_ITEMS)
    }


def test_default_profile_renders_backing_and_full_temporal(chart_dir: str) -> None:
    documents = render(Path(chart_dir))
    assert all(
        document["metadata"]["labels"]["app.kubernetes.io/instance"] == RELEASE
        for document in documents
        if document["kind"] == "Deployment"
    )
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
    resource(documents, "Deployment", "raven-platform-workers")
    resource(documents, "Deployment", "raven-platform-langfuse")
    resource(documents, "Deployment", f"{RELEASE}-temporal-frontend")


def test_nondefault_names_ports_and_consumers(chart_dir: str) -> None:
    documents = render(
        Path(chart_dir),
        {
            "fullnameOverride": "control-plane",
            "temporal.enabled": False,
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


def test_full_temporal_uses_v1_sql_datastores_without_hooks(chart_dir: str) -> None:
    documents = render(
        Path(chart_dir),
        {
            "fullnameOverride": "control-plane",
            "temporal.server.frontend.service.port": 7338,
        },
    )
    config = resource(documents, "ConfigMap", "control-plane-temporal-config")
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
        "passwordCommand:",
        "/bin/printenv",
    ):
        assert expected in rendered_config
    assert "password:" not in rendered_config
    assert "existingSecret:" not in rendered_config
    assert "driver: sql" not in rendered_config
    jobs = [doc for doc in documents if doc["kind"] == "Job"]
    assert len(jobs) == 2
    assert all(
        "helm.sh/hook" not in job["metadata"].get("annotations", {}) for job in jobs
    )


@pytest.mark.parametrize(
    "settings,expected_fragments",
    [
        (
            {"temporal.enabled": True, "temporalDev.enabled": True},
            ("mutually exclusive",),
        ),
        (
            {
                "temporal.enabled": False,
                "temporalDev.enabled": False,
                "workers.enabled": True,
            },
            ("requires one Temporal profile",),
        ),
        (
            {"frontend.service.port": 70000},
            (
                "values don't meet the specifications of the schema",
                "/frontend/service/port",
            ),
        ),
        (
            {"workers.health.port": 0},
            (
                "values don't meet the specifications of the schema",
                "/workers/health/port",
            ),
        ),
        (
            {"workers.health.port": 70000},
            (
                "values don't meet the specifications of the schema",
                "/workers/health/port",
            ),
        ),
        (
            {"temporal.server.frontend.service.port": 70000},
            (
                "values don't meet the specifications of the schema",
                "/temporal/server/frontend/service/port",
            ),
        ),
        (
            {"database.port": 5544},
            (
                "values don't meet the specifications of the schema",
                "/database/port",
            ),
        ),
        (
            {"database.substrateUser": "postgres"},
            (
                "values don't meet the specifications of the schema",
                "/database/substrateUser",
            ),
        ),
        (
            {"database.substrateDatabase": "postgres"},
            (
                "values don't meet the specifications of the schema",
                "/database/substrateDatabase",
            ),
        ),
    ],
)
def test_invalid_profiles_fail(
    chart_dir: str, settings: dict[str, Any], expected_fragments: tuple[str, ...]
) -> None:
    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(Path(chart_dir), settings)
    for fragment in expected_fragments:
        assert fragment in captured.value.stderr


def test_values_schema_rejects_unknown_properties_and_coerced_types(
    chart_dir: str,
) -> None:
    platform_chart = Path(chart_dir)
    substrate_chart = platform_chart.parent / "agent-substrate"

    for chart, unknown_key in (
        (platform_chart, "unknownKey"),
        (platform_chart, "unifiedApi.unknownKey"),
        (platform_chart, "temporal.unknownKey"),
        (substrate_chart, "unknownKey"),
        (substrate_chart, "ateapi.unknownKey"),
    ):
        with pytest.raises(subprocess.CalledProcessError) as captured:
            render(chart, {unknown_key: True})
        assert "values don't meet the specifications of the schema" in (
            captured.value.stderr
        )
        assert "additional properties 'unknownKey' not allowed" in (
            captured.value.stderr
        )

    for chart, flag_val, expected_type_err in (
        (platform_chart, "frontend.service.port=3000", "got string, want integer"),
        (platform_chart, "unifiedApi.enabled=true", "got string, want boolean"),
        (substrate_chart, "ateapi.service.port=50051", "got string, want integer"),
    ):
        with pytest.raises(subprocess.CalledProcessError) as captured:
            subprocess.run(
                [
                    "helm",
                    "template",
                    RELEASE,
                    str(chart),
                    "--namespace",
                    NAMESPACE,
                    "--set-string",
                    flag_val,
                ],
                text=True,
                capture_output=True,
                check=True,
            )
        assert "values don't meet the specifications of the schema" in (
            captured.value.stderr
        )
        assert expected_type_err in captured.value.stderr

    for settings, pointer in (
        ({"ateapi.service.port": 0}, "/ateapi/service/port"),
        ({"database.port": 70000}, "/database/port"),
        ({"rustfs.port": 0}, "/rustfs/port"),
    ):
        with pytest.raises(subprocess.CalledProcessError) as captured:
            render(substrate_chart, settings)
        assert "values don't meet the specifications of the schema" in (
            captured.value.stderr
        )
        assert pointer in captured.value.stderr

    with pytest.raises(subprocess.CalledProcessError) as captured:
        subprocess.run(
            [
                "helm",
                "template",
                RELEASE,
                str(substrate_chart),
                "--namespace",
                "ate-system",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
    assert "must NOT be deployed into the 'ate-system' namespace" in (
        captured.value.stderr
    )

    with pytest.raises(subprocess.CalledProcessError) as captured:
        render(substrate_chart, {"global.tls.useOcpServingCert": True})
    assert "OpenShift serving-cert mode requires tls.existingBundleSecret" in (
        captured.value.stderr
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
    assert (
        "--postgres-read-write-connection-string=$(POSTGRES_URL)" in container["args"]
    )
    assert "--postgres-owner-connection-string=$(POSTGRES_URL)" in container["args"]
    assert "--postgres-read-write-role=sandbox_role" in container["args"]
    assert "--postgres-owner-role=sandbox_role" in container["args"]
    assert (
        environment(container)["AWS_ENDPOINT_URL_S3"]
        == "http://raven-platform-rustfs.backing.svc.cluster.local:9195"
    )
    controller_deployment = resource(
        documents, "Deployment", "raven-agent-substrate-atecontroller"
    )
    assert "annotations" not in controller_deployment["metadata"]
    controller = controller_deployment["spec"]["template"]["spec"]["containers"][0]
    assert controller["ports"] == [{"containerPort": 8080, "name": "metrics"}]
    assert controller["readinessProbe"]["httpGet"] == {
        "path": "/metrics",
        "port": "metrics",
    }
    assert controller["livenessProbe"]["httpGet"] == {
        "path": "/metrics",
        "port": "metrics",
    }
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
    leaf.verify_directly_issued_by(authority)


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
    if secret_template != "secret-tls.yaml":
        (chart / "templates" / "secret-tls.yaml").rename(
            chart / "templates" / secret_template
        )
    documents = render(chart)
    assert_substrate_bundle_matches_pinned_loader(
        resource(documents, "Secret", "raven-agent-substrate-tls")
    )
    assert_substrate_tls_checksums_match_secret(documents)


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


def test_provider_spilo_identity_can_read_group_owned_tls(chart_dir: str) -> None:
    values = yaml.safe_load(
        (Path(chart_dir).parent / "postgres-operator-values.yaml").read_text()
    )
    configuration = values["configKubernetes"]
    assert configuration["enable_readiness_probe"] is True
    assert configuration["spilo_runasuser"] == 101
    assert configuration["spilo_runasgroup"] == 103
    assert configuration["spilo_fsgroup"] == 103


def test_chart_values_use_renovate_compatible_image_mappings(chart_dir: str) -> None:
    charts_root = Path(chart_dir).parent

    def check_images(node: Any, path: str = "") -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            current = f"{path}.{key}" if path else key
            if key == "image":
                assert isinstance(value, dict), (
                    f"{current} must be a {{repository, tag}} mapping for Renovate helm-values"
                )
                if path.startswith("platform/"):
                    assert not current.endswith(".global.image"), (
                        f"{current} must not use a global image prefix in platform values"
                    )
                    assert {"repository", "tag"} <= set(value), (
                        f"{current} must define repository and tag"
                    )
                    assert "/" in str(value["repository"]).removeprefix("ghcr.io/"), (
                        f"{current}.repository must be a complete image path, not a registry/org prefix"
                    )
                else:
                    assert "repository" in value or "tag" in value, (
                        f"{current} must define repository or tag"
                    )
            else:
                check_images(value, current)

    for values_file in sorted(charts_root.glob("*/values*.yaml")):
        rel = f"{values_file.parent.name}/{values_file.name}"
        check_images(yaml.safe_load(values_file.read_text()), rel)
