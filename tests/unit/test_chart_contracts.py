"""Independent rendered contracts; these tests never deploy or run containers."""

import base64
import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypedDict, Unpack

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization

Manifest = dict[str, Any]
Setting = str | int | bool
EMPTY_ITEMS: tuple[object, ...] = ()
RELEASE = "raven"
NAMESPACE = "lab-two"
FULLNAME = f"{RELEASE}-platform"
PLATFORM = "platform"
SUBSTRATE = "agent-substrate"
SCHEMA_ERROR = "values don't meet the specifications of the schema"


class HelmOptions(TypedDict, total=False):
    string_settings: Mapping[str, str]
    json_settings: Mapping[str, Any]
    values_files: Sequence[Path]
    release: str
    namespace: str


def helm_template(
    chart: Path,
    settings: Mapping[str, Setting] | None = None,
    **options: Unpack[HelmOptions],
) -> subprocess.CompletedProcess[str]:
    command = [
        "helm",
        "template",
        options.get("release", RELEASE),
        str(chart),
        "--namespace",
        options.get("namespace", NAMESPACE),
    ]
    for values_file in options.get("values_files", ()):
        command.extend(["--values", str(values_file)])
    for key, value in (settings or {}).items():
        serialized = str(value).lower() if isinstance(value, bool) else str(value)
        command.extend(["--set", f"{key}={serialized}"])
    for key, text in options.get("string_settings", {}).items():
        command.extend(["--set-string", f"{key}={text}"])
    for key, data in options.get("json_settings", {}).items():
        command.extend(["--set-json", f"{key}={json.dumps(data)}"])
    return subprocess.run(command, text=True, capture_output=True, check=False)


def render(
    chart: Path,
    settings: Mapping[str, Setting] | None = None,
    **options: Unpack[HelmOptions],
) -> list[Manifest]:
    result = helm_template(chart, settings, **options)
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def render_error(
    chart: Path,
    settings: Mapping[str, Setting] | None = None,
    **options: Unpack[HelmOptions],
) -> str:
    result = helm_template(chart, settings, **options)
    assert result.returncode != 0, "helm template unexpectedly succeeded"
    return result.stderr


def chart_path(chart_dir: str, name: str) -> Path:
    return Path(chart_dir).parent / name


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
    resource(documents, "ServiceAccount", f"{FULLNAME}-temporal-schema")
    resource(documents, "Secret", "raven-db-tls")
    trust = resource(documents, "ConfigMap", "raven-db-trust")
    assert set(trust["data"]) == {"ca.crt"}
    assert "PRIVATE KEY" not in trust["data"]["ca.crt"]
    resource(documents, "Deployment", "raven-platform-workers")
    resource(documents, "Deployment", "raven-platform-langfuse")
    resource(documents, "Deployment", TEMPORAL_FRONTEND)


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


def test_temporal_config_uses_sql_datastores_with_password_command(
    chart_dir: str,
) -> None:
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
        "grpcPort: 7338",
    ):
        assert expected in rendered_config
    assert "password:" not in rendered_config
    assert "existingSecret:" not in rendered_config
    assert "driver: sql" not in rendered_config


POD_KINDS = ("Deployment", "Job")
TEMPORAL_ROLES = ("frontend", "history", "matching", "worker")
TEMPORAL_LABELS = {
    "app.kubernetes.io/name": "temporal",
    "app.kubernetes.io/instance": RELEASE,
}
JOB_LABELS = {**TEMPORAL_LABELS, "app.kubernetes.io/component": "database"}
TEMPORAL_FRONTEND = f"{RELEASE}-temporal-frontend"
POD_SECURITY: Manifest = {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "runAsGroup": 10001,
    "fsGroup": 10001,
    "seccompProfile": {"type": "RuntimeDefault"},
}
CONTAINER_SECURITY: Manifest = {
    "runAsNonRoot": True,
    "readOnlyRootFilesystem": True,
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
}


def workload(component: str) -> str:
    return f"{FULLNAME}-{component}"


def pod_templates(documents: list[Manifest]) -> dict[str, Manifest]:
    """Pod templates of every Deployment and Job, keyed by workload name."""
    return {
        document["metadata"]["name"]: document["spec"]["template"]
        for document in documents
        if document["kind"] in POD_KINDS
    }


def selector_matches(selector: Mapping[str, str], labels: Mapping[str, str]) -> bool:
    """Equality-selector semantics; an empty selector deliberately matches nothing."""
    return bool(selector) and selector.items() <= labels.items()


def selected_workloads(
    documents: list[Manifest], selector: Mapping[str, str]
) -> set[str]:
    return {
        name
        for name, template in pod_templates(documents).items()
        if selector_matches(selector, template["metadata"].get("labels", {}))
    }


def pod_containers(template: Manifest) -> list[Manifest]:
    spec = template["spec"]
    return [*spec.get("initContainers", []), *spec["containers"]]


TEMPORAL_CREDENTIAL = f"temporal.{RELEASE}-db.credentials.postgresql.acid.zalan.do"
TEMPORAL_SECRET_REF = {"secretKeyRef": {"name": TEMPORAL_CREDENTIAL, "key": "password"}}


def names_of(documents: list[Manifest], kind: str) -> set[str]:
    return {
        document["metadata"]["name"]
        for document in documents
        if document["kind"] == kind
    }


def temporal_deployments(documents: list[Manifest]) -> dict[str, Manifest]:
    """First-party Temporal server Deployments keyed by role."""
    return {
        document["metadata"]["labels"]["app.kubernetes.io/component"]: document
        for document in documents
        if document["kind"] == "Deployment"
        and selector_matches(
            TEMPORAL_LABELS, document["spec"]["template"]["metadata"]["labels"]
        )
    }


def temporal_jobs(documents: list[Manifest]) -> dict[str, Manifest]:
    return {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "Job"
    }


def frontend_services(documents: list[Manifest]) -> tuple[Manifest, Manifest]:
    """The UI frontend Service and the Temporal frontend Service (not headless)."""
    services = [document for document in documents if document["kind"] == "Service"]
    (ui,) = (s for s in services if s["spec"]["selector"].get("app") == "frontend")
    (temporal,) = (
        service
        for service in services
        if service["spec"]["selector"].get("app.kubernetes.io/component") == "frontend"
        and not service["metadata"]["name"].endswith("-headless")
    )
    return ui, temporal


def test_temporal_server_deployments_contract(chart_dir: str) -> None:
    documents = render(Path(chart_dir))
    config = resource(documents, "ConfigMap", f"{RELEASE}-temporal-config")["data"][
        "config_template.yaml"
    ]
    deployments = temporal_deployments(documents)
    assert set(deployments) == set(TEMPORAL_ROLES)
    assert 'env "POD_IP"' in config
    for role, deployment in deployments.items():
        name = deployment["metadata"]["name"]
        assert name == f"{RELEASE}-temporal-{role}"
        selector = deployment["spec"]["selector"]["matchLabels"]
        assert selector["app.kubernetes.io/component"] == role
        assert selected_workloads(documents, selector) == {name}
        (container,) = deployment["spec"]["template"]["spec"]["containers"]
        env = environment(container)
        port_names = {port["name"] for port in container["ports"]}
        for probe in ("readinessProbe", "livenessProbe"):
            assert container[probe]["tcpSocket"]["port"] in port_names
        assert env["TEMPORAL_SERVICES"] == role
        assert env["POD_IP"] == {
            "fieldRef": {"apiVersion": "v1", "fieldPath": "status.podIP"}
        }
        for variable in (
            "TEMPORAL_DEFAULT_STORE_PASSWORD",
            "TEMPORAL_VISIBILITY_STORE_PASSWORD",
        ):
            assert env[variable] == TEMPORAL_SECRET_REF
            assert variable in config
        # The entrypoint reads only TEMPORAL_SERVICES, and the config template
        # resolves the broadcast address from POD_IP.
        assert not {"SERVICES", "TEMPORAL_BROADCAST_ADDRESS"} & set(env)


def test_temporal_pods_are_hardened(chart_dir: str) -> None:
    documents = render(Path(chart_dir))
    service_account = f"{FULLNAME}-temporal-schema"
    workloads = [
        *temporal_deployments(documents).values(),
        *temporal_jobs(documents).values(),
    ]
    assert len(workloads) == len(TEMPORAL_ROLES) + 2
    for item in workloads:
        template = item["spec"]["template"]
        pod = template["spec"]
        assert pod["serviceAccountName"] == service_account
        assert pod["automountServiceAccountToken"] is False
        assert POD_SECURITY.items() <= pod["securityContext"].items()
        for container in pod_containers(template):
            assert CONTAINER_SECURITY.items() <= container["securityContext"].items()
            env = environment(container)
            assert env["HOME"] == "/tmp"
            assert env["USER"] == "temporal"
            assert "/tmp" in {mount["mountPath"] for mount in container["volumeMounts"]}


def test_temporal_services_resolve_to_container_ports(chart_dir: str) -> None:
    documents = render(Path(chart_dir))
    services = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "Service"
        and selector_matches(TEMPORAL_LABELS, document["spec"]["selector"])
    }
    assert set(services) == {
        TEMPORAL_FRONTEND,
        *(f"{RELEASE}-temporal-{role}-headless" for role in TEMPORAL_ROLES),
    }
    templates = pod_templates(documents)
    for name, service in services.items():
        targets = selected_workloads(documents, service["spec"]["selector"])
        assert len(targets) == 1, name
        container_ports = {
            port["name"]: port["containerPort"]
            for container in templates[next(iter(targets))]["spec"]["containers"]
            for port in container["ports"]
        }
        for port in service["spec"]["ports"]:
            assert container_ports[port["targetPort"]] == port["port"], (name, port)
        headless = name.endswith("-headless")
        assert (service["spec"].get("clusterIP") == "None") is headless, name
        assert service["spec"].get("publishNotReadyAddresses", False) is headless, name


def test_temporal_jobs_contract(chart_dir: str) -> None:
    documents = render(Path(chart_dir))
    jobs = temporal_jobs(documents)
    assert set(jobs) == {
        f"{RELEASE}-temporal-schema-1",
        f"{RELEASE}-temporal-namespace-1",
    }
    for job in jobs.values():
        # Checkov CKV2_K8S_6 and the temporal-netpol selector depend on pod labels.
        assert JOB_LABELS.items() <= job["metadata"]["labels"].items()
        assert (
            JOB_LABELS.items() <= job["spec"]["template"]["metadata"]["labels"].items()
        )
        assert "helm.sh/hook" not in job["metadata"].get("annotations", {})
        assert job["spec"]["template"]["spec"]["restartPolicy"] == "OnFailure"

    schema = jobs[f"{RELEASE}-temporal-schema-1"]["spec"]["template"]["spec"]
    init = {container["name"]: container for container in schema["initContainers"]}
    assert list(init) == ["setup-default-store", "setup-visibility-store"]
    assert [container["name"] for container in schema["containers"]] == ["done"]
    for store, database, versioned in (
        ("setup-default-store", "temporal", "/temporal/versioned"),
        ("setup-visibility-store", "temporal_visibility", "/visibility/versioned"),
    ):
        script = " ".join(init[store]["args"])
        assert set(re.findall(r"--database (\w+)", script)) == {database}, store
        # setup-schema overwrites the stored version when a version read fails, so it
        # must only run as the fallback for a missing schema_version table.
        assert script.index("update-schema") < script.index("setup-schema -v 0.0"), (
            store
        )
        assert "*schema_version*" in script, store
        assert '*"no rows in result set"*' in script, store
        assert "*) exit 1 ;;" in script, store
        assert versioned in script
        env = environment(init[store])
        assert env["SQL_USER"] == "temporal"
        assert env["SQL_PASSWORD"] == TEMPORAL_SECRET_REF
        assert env["SQL_HOST"] == f"{RELEASE}-db.{NAMESPACE}.svc.cluster.local"
        assert env["SQL_PORT"] == "5432"
        assert env["SQL_TLS"] == "true"
        assert env["SQL_TLS_DISABLE_HOST_VERIFICATION"] == "false"

    (create,) = jobs[f"{RELEASE}-temporal-namespace-1"]["spec"]["template"]["spec"][
        "containers"
    ]
    assert (
        environment(create)["TEMPORAL_ADDRESS"]
        == f"{TEMPORAL_FRONTEND}.{NAMESPACE}.svc.cluster.local:7233"
    )


def test_temporal_namespace_job_follows_values(chart_dir: str) -> None:
    chart = Path(chart_dir)
    enabled = render(
        chart,
        {
            "workers.temporalNamespace": "tenant-one",
            "temporal.server.config.namespaces.retention": "72h",
        },
    )
    job = resource(enabled, "Job", f"{RELEASE}-temporal-namespace-1")
    rendered = json.dumps(job["spec"]["template"]["spec"]["containers"])
    assert "tenant-one" in rendered
    assert "72h" in rendered
    disabled = render(chart, {"temporal.server.config.namespaces.create": False})
    assert names_of(disabled, "Job") == {f"{RELEASE}-temporal-schema-1"}


def zone_affinity(zone: str) -> Manifest:
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {"key": "zone", "operator": "In", "values": [zone]}
                        ]
                    }
                ]
            }
        }
    }


def scheduling_values(group: str) -> dict[str, Any]:
    return {
        "nodeSelector": {"pool": group},
        "tolerations": [{"key": group, "operator": "Exists"}],
        "affinity": zone_affinity(group),
        "podAnnotations": {"team": group},
    }


def test_temporal_scheduling_values_reach_their_own_pods(chart_dir: str) -> None:
    documents = render(
        Path(chart_dir),
        json_settings={
            f"temporal.{group}.{key}": value
            for group in ("server", "schema")
            for key, value in scheduling_values(group).items()
        },
    )
    templates = pod_templates(documents)
    expected = {
        **{f"{RELEASE}-temporal-{role}": "server" for role in TEMPORAL_ROLES},
        f"{RELEASE}-temporal-schema-1": "schema",
        f"{RELEASE}-temporal-namespace-1": "schema",
    }
    for name, group in expected.items():
        values = scheduling_values(group)
        template = templates[name]
        assert template["spec"]["nodeSelector"] == values["nodeSelector"], name
        assert template["spec"]["tolerations"] == values["tolerations"], name
        assert template["spec"]["affinity"] == values["affinity"], name
        assert template["metadata"]["annotations"] == values["podAnnotations"], name


def long_name(seed: str, length: int) -> str:
    padding = "x" * length
    return f"{seed}-{padding}"[:length]


# Helm rejects release names over 53 characters, so very long names also arrive
# through fullnameOverride.
LONG_NAME_CASES: list[Any] = [
    *(
        pytest.param({}, long_name(seed, length), id=f"release-{seed}-{length}")
        for seed in ("platform", "temporal", "stack")
        for length in (40, 44, 45, 46, 53)
    ),
    *(
        pytest.param(
            {"fullnameOverride": long_name(seed, length)},
            RELEASE,
            id=f"fullname-{seed}-{length}",
        )
        for seed in ("platform", "temporal")
        for length in (44, 45, 46, 63)
    ),
    pytest.param({}, "platform-temporal", id="release-platform-temporal"),
]


@pytest.mark.parametrize("settings,release", LONG_NAME_CASES)
def test_long_names_stay_within_limits_and_unique(
    chart_dir: str, settings: dict[str, Setting], release: str
) -> None:
    documents = render(Path(chart_dir), settings, release=release)
    identities = [
        (
            document["kind"],
            document["metadata"].get("namespace", NAMESPACE),
            document["metadata"]["name"],
        )
        for document in documents
    ]
    assert [item for item, count in Counter(identities).items() if count > 1] == []
    assert [item for item in identities if len(item[2]) > 63] == []
    ui_frontend, temporal_frontend = frontend_services(documents)
    assert ui_frontend["metadata"]["name"] != temporal_frontend["metadata"]["name"]
    address = (
        f"{temporal_frontend['metadata']['name']}.{NAMESPACE}.svc.cluster.local:7233"
    )
    worker = next(
        document
        for document in documents
        if document["kind"] == "Deployment"
        and document["metadata"]["labels"].get("app") == "workers"
    )
    assert (
        environment(worker["spec"]["template"]["spec"]["containers"][0])["TEMPORAL_URL"]
        == address
    )
    job = next(
        document
        for document in documents
        if document["kind"] == "Job"
        and document["metadata"]["name"].endswith("-namespace-1")
    )
    assert (
        environment(job["spec"]["template"]["spec"]["containers"][0])[
            "TEMPORAL_ADDRESS"
        ]
        == address
    )


# 45 is the longest base reused as is; one more character forces truncation.
REUSED_45 = "temporal-" + "x" * 36
TRUNCATED_46 = "temporal-" + "x" * 37
TRIMMED_46 = "a" * 35 + "-" + "b" * 10


@pytest.mark.parametrize(
    "release,fullname_override,expected",
    [
        ("raven", None, "raven-temporal-frontend"),
        # A base that already contains "temporal" is reused unless it is the UI fullname.
        ("temporal", None, "temporal-frontend"),
        ("my-temporal", None, "my-temporal-frontend"),
        (REUSED_45, None, f"{REUSED_45}-frontend"),
        (TRUNCATED_46, None, f"temporal-{'x' * 27}-temporal-frontend"),
        (TRIMMED_46, None, f"{'a' * 35}-temporal-frontend"),
        ("temporal-platform", None, "temporal-platform-temporal-frontend"),
        ("platform", None, "platform-temporal-frontend"),
        (RELEASE, "control-plane", "control-plane-temporal-frontend"),
        (RELEASE, "temporal-x", "temporal-x-temporal-frontend"),
        (RELEASE, "temporal", "temporal-temporal-frontend"),
    ],
)
def test_temporal_frontend_name_reuses_release_name_only_when_distinct_from_ui(
    chart_dir: str, release: str, fullname_override: str | None, expected: str
) -> None:
    settings: dict[str, Setting] = (
        {} if fullname_override is None else {"fullnameOverride": fullname_override}
    )
    documents = render(Path(chart_dir), settings, release=release)
    ui_frontend, temporal_frontend = frontend_services(documents)
    assert temporal_frontend["metadata"]["name"] == expected
    assert ui_frontend["metadata"]["name"] != expected


@pytest.mark.parametrize(
    "settings,frontend,label",
    [
        ({"temporal.nameOverride": "custom"}, f"{RELEASE}-custom-frontend", "custom"),
        (
            {"temporal.nameOverride": "n" * 62},
            f"{RELEASE}-{'n' * 20}-frontend",
            "n" * 62,
        ),
        ({"temporal.fullnameOverride": "f" * 63}, f"{'f' * 45}-frontend", "temporal"),
    ],
    ids=["name-override", "long-name-override", "long-fullname-override"],
)
def test_temporal_name_overrides_flow_through_names_labels_and_selectors(
    chart_dir: str, settings: dict[str, Setting], frontend: str, label: str
) -> None:
    documents = render(Path(chart_dir), settings)
    _, temporal_frontend = frontend_services(documents)
    assert temporal_frontend["metadata"]["name"] == frontend
    selector = {"app.kubernetes.io/name": label, "app.kubernetes.io/instance": RELEASE}
    temporal_pods = selected_workloads(documents, selector)
    assert len(temporal_pods) == len(TEMPORAL_ROLES) + 2
    assert all(
        name.startswith(frontend.removesuffix("-frontend")) for name in temporal_pods
    )
    policy = resource(documents, "NetworkPolicy", workload("temporal-netpol"))
    assert policy["spec"]["podSelector"]["matchLabels"] == selector
    address = f"{frontend}.{NAMESPACE}.svc.cluster.local:7233"
    worker = resource(documents, "Deployment", workload("workers"))
    (container,) = worker["spec"]["template"]["spec"]["containers"]
    assert environment(container)["TEMPORAL_URL"] == address


@pytest.mark.parametrize(
    "release,settings",
    [
        ("raven", {"nameOverride": "temporal"}),
        ("raven", {"temporal.nameOverride": "platform"}),
        ("raven", {"temporal.fullnameOverride": "raven-platform"}),
        ("platform", {"temporal.nameOverride": "litellm"}),
        ("platform", {"temporal.nameOverride": "caddy"}),
        ("platform", {"temporal.fullnameOverride": "platform-caddy"}),
    ],
    ids=[
        "chart-name-override-equals-temporal",
        "temporal-name-equals-chart-name",
        "temporal-fullname-equals-ui-fullname",
        "temporal-name-equals-component",
        "temporal-name-prefix-of-component-config",
        "temporal-fullname-prefix-of-component-config",
    ],
)
def test_temporal_names_that_collide_with_platform_names_are_rejected(
    chart_dir: str, release: str, settings: dict[str, Setting]
) -> None:
    stderr = render_error(Path(chart_dir), settings, release=release)
    assert "would collide with platform resources" in stderr


def platform_components(chart: Path) -> set[str]:
    """Component names the chart uses to name platform objects, read from its templates."""
    names: set[str] = set()
    for path in (chart / "templates").iterdir():
        if path.name != "temporal.yaml" and path.suffix in {".yaml", ".tpl"}:
            names |= set(re.findall(r'"component" "([a-z-]+)"', path.read_text()))
    return names


TEMPORAL_SUFFIXES = (
    "config",
    "dynamic-config",
    "frontend",
    "history",
    "matching",
    "worker",
    "frontend-headless",
    "history-headless",
    "matching-headless",
    "worker-headless",
)


def collision_prone_temporal_names(components: set[str]) -> set[str]:
    """Temporal base names ("" = the UI fullname itself) for which some
    `<temporal>-<suffix>` equals some `<ui>-<component>`."""
    names: set[str] = set()
    for component in components:
        for suffix in TEMPORAL_SUFFIXES:
            if component == suffix:
                names.add("")
            elif component.endswith(f"-{suffix}"):
                names.add(component.removesuffix(f"-{suffix}"))
    return names


@pytest.mark.parametrize("form", ["name-override", "fullname-override"])
def test_no_temporal_override_collides_with_any_platform_component(
    chart_dir: str, form: str
) -> None:
    """A colliding name must fail at render; anything that renders must be unique."""
    chart = Path(chart_dir)
    components = platform_components(chart)
    assert {"caddy-config", "litellm-config", "unified-api"} <= components
    candidates = collision_prone_temporal_names(components)
    assert {"", "caddy", "litellm"} <= candidates
    for candidate in sorted(candidates):
        if form == "name-override":
            if not candidate:
                continue  # an empty name cannot be expressed as a name override
            settings: dict[str, Setting] = {"temporal.nameOverride": candidate}
        else:
            base = f"{PLATFORM}-{candidate}" if candidate else PLATFORM
            settings = {"temporal.fullnameOverride": base}
        release = PLATFORM  # UI fullname equals the release name
        component = candidate or "(ui fullname)"
        result = helm_template(chart, settings, release=release)
        if result.returncode != 0:
            assert "would collide with platform resources" in result.stderr, component
            continue
        identities = [
            (
                document["kind"],
                document["metadata"].get("namespace", NAMESPACE),
                document["metadata"]["name"],
            )
            for document in yaml.safe_load_all(result.stdout)
            if document
        ]
        duplicates = [item for item, count in Counter(identities).items() if count > 1]
        assert duplicates == [], component


def test_temporal_name_collision_check_only_applies_to_the_full_profile(
    chart_dir: str,
) -> None:
    documents = render(
        Path(chart_dir),
        {
            "temporal.enabled": False,
            "temporalDev.enabled": True,
            "temporal.nameOverride": "platform",
        },
    )
    assert not temporal_deployments(documents)


def test_temporal_frontend_port_override_reaches_every_consumer(chart_dir: str) -> None:
    documents = render(Path(chart_dir), {"temporal.server.frontend.service.port": 7338})
    deployment = temporal_deployments(documents)["frontend"]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    assert {port["name"]: port["containerPort"] for port in container["ports"]}[
        "rpc"
    ] == 7338
    _, temporal_frontend = frontend_services(documents)
    assert {port["name"]: port["port"] for port in temporal_frontend["spec"]["ports"]}[
        "grpc-rpc"
    ] == 7338
    address = f"{TEMPORAL_FRONTEND}.{NAMESPACE}.svc.cluster.local:7338"
    (create,) = resource(documents, "Job", f"{RELEASE}-temporal-namespace-1")["spec"][
        "template"
    ]["spec"]["containers"]
    assert environment(create)["TEMPORAL_ADDRESS"] == address
    worker = resource(documents, "Deployment", workload("workers"))
    (worker_container,) = worker["spec"]["template"]["spec"]["containers"]
    assert environment(worker_container)["TEMPORAL_URL"] == address
    temporal_policy = resource(documents, "NetworkPolicy", workload("temporal-netpol"))
    for direction in ("ingress", "egress"):
        ports = {port for _, port in policy_ports(temporal_policy, direction)}
        assert 7338 in ports
        assert 7233 not in ports
    for component in ("unified-api-ingress", "workers-netpol"):
        policy = resource(documents, "NetworkPolicy", workload(component))
        assert allowed_workloads(documents, policy, "egress", 7338) == {
            TEMPORAL_FRONTEND
        }
        assert not rules_for_port(policy, "egress", 7233)


DATABASE_SELECTOR = {"cluster-name": f"{RELEASE}-db"}
PRIVATE_CIDRS = {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"}
PEER_KEYS = {"ingress": "from", "egress": "to"}
TEMPORAL_RING_PORTS = (7234, 7235, 6933, 6934, 6935, 6939)
NETWORK_POLICY_PROFILES = ("default", "kind", "temporal-dev")


def render_profile(chart_dir: str, profile: str) -> list[Manifest]:
    chart = Path(chart_dir)
    if profile == "kind":
        return render(chart, values_files=[chart / "values-kind.yaml"])
    if profile == "temporal-dev":
        return render(chart, {"temporal.enabled": False, "temporalDev.enabled": True})
    return render(chart)


def rule_ports(rule: Manifest) -> set[int]:
    return {entry["port"] for entry in rule.get("ports", []) if "port" in entry}


def rules_for_port(policy: Manifest, direction: str, port: int) -> list[Manifest]:
    return [
        rule for rule in policy["spec"].get(direction) or [] if port in rule_ports(rule)
    ]


def policy_ports(policy: Manifest, direction: str) -> set[tuple[str, int]]:
    """Every (protocol, port) a policy opens in one direction."""
    return {
        (entry["protocol"], entry["port"])
        for rule in policy["spec"].get(direction) or []
        for entry in rule.get("ports", [])
        if "port" in entry
    }


def peer_kinds(policy: Manifest, direction: str) -> set[str]:
    """Selector kinds (podSelector, namespaceSelector, ipBlock) used as peers."""
    return {
        kind
        for rule in policy["spec"].get(direction) or []
        for peer in rule.get(PEER_KEYS[direction], [])
        for kind in peer
    }


def allowed_workloads(
    documents: list[Manifest], policy: Manifest, direction: str, port: int
) -> set[str]:
    """In-release workloads reachable on `port` through same-namespace pod peers."""
    allowed: set[str] = set()
    for rule in rules_for_port(policy, direction, port):
        for peer in rule.get(PEER_KEYS[direction], []):
            if "podSelector" in peer and "namespaceSelector" not in peer:
                labels = peer["podSelector"].get("matchLabels", {})
                allowed |= selected_workloads(documents, labels)
    return allowed


def allowed_namespaces(policy: Manifest, direction: str, port: int) -> set[str]:
    return {
        peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
        for rule in rules_for_port(policy, direction, port)
        for peer in rule.get(PEER_KEYS[direction], [])
        if "namespaceSelector" in peer
    }


def egress_ports_to(policy: Manifest, selector: Mapping[str, str]) -> set[int]:
    return {
        port
        for rule in policy["spec"].get("egress") or []
        for peer in rule.get("to", [])
        if peer.get("podSelector", {}).get("matchLabels") == selector
        and "namespaceSelector" not in peer
        for port in rule_ports(rule)
    }


def public_egress_ports(policy: Manifest) -> set[int]:
    ports: set[int] = set()
    for rule in policy["spec"].get("egress") or []:
        for peer in rule.get("to", []):
            if "ipBlock" in peer:
                assert peer["ipBlock"]["cidr"] == "0.0.0.0/0"
                assert PRIVATE_CIDRS <= set(peer["ipBlock"]["except"])
                ports |= rule_ports(rule)
    return ports


def assert_peer_is_scoped(
    documents: list[Manifest], policy_name: str, peer: Manifest
) -> None:
    pod, namespace = peer.get("podSelector"), peer.get("namespaceSelector")
    if "ipBlock" in peer:
        assert pod is None and namespace is None, policy_name
        if peer["ipBlock"]["cidr"] == "0.0.0.0/0":
            assert PRIVATE_CIDRS <= set(peer["ipBlock"].get("except", [])), policy_name
        return
    assert pod is not None or namespace is not None, f"{policy_name}: empty peer"
    if namespace is not None:
        assert namespace.get("matchLabels"), (
            f"{policy_name}: namespaceSelector selects every namespace"
        )
    if pod is not None:
        assert pod.get("matchLabels"), f"{policy_name}: podSelector selects every pod"
    if pod is not None and namespace is None:
        labels = pod["matchLabels"]
        if "cluster-name" in labels:  # Spilo/Zalando pods have no chart template
            assert labels == DATABASE_SELECTOR, policy_name
            resource(documents, "postgresql", DATABASE_SELECTOR["cluster-name"])
        else:
            assert selected_workloads(documents, labels), (
                f"{policy_name}: peer {labels} matches no rendered workload"
            )


@pytest.mark.parametrize("profile", NETWORK_POLICY_PROFILES)
def test_network_policies_are_scoped_to_real_pods(chart_dir: str, profile: str) -> None:
    documents = render_profile(chart_dir, profile)
    policies = [d for d in documents if d["kind"] == "NetworkPolicy"]
    assert policies
    covered: set[str] = set()
    for policy in policies:
        name, spec = policy["metadata"]["name"], policy["spec"]
        assert set(spec["policyTypes"]) == {"Ingress", "Egress"}, name
        target = spec["podSelector"].get("matchLabels")
        assert target, f"{name}: empty podSelector selects every pod"
        selected = selected_workloads(documents, target)
        assert selected, f"{name}: podSelector matches no rendered workload"
        covered |= selected
        for direction, key in PEER_KEYS.items():
            for rule in spec.get(direction) or []:
                assert rule.get("ports"), f"{name}: {direction} rule opens every port"
                assert rule.get(key), f"{name}: {direction} rule allows every peer"
                for peer in rule[key]:
                    assert_peer_is_scoped(documents, name, peer)
    uncovered = set(pod_templates(documents)) - covered
    assert not uncovered, f"workloads without a NetworkPolicy: {uncovered}"


def test_service_network_policy_contracts(chart_dir: str) -> None:
    documents = render(Path(chart_dir))
    api, workers = workload("unified-api"), workload("workers")
    litellm, langfuse = workload("litellm"), workload("langfuse")

    def policy(component: str) -> Manifest:
        return resource(documents, "NetworkPolicy", workload(component))

    litellm_policy = policy("litellm-netpol")
    assert allowed_workloads(documents, litellm_policy, "ingress", 4000) == {
        api,
        workers,
    }
    assert allowed_workloads(documents, litellm_policy, "egress", 3000) == {langfuse}
    assert allowed_namespaces(litellm_policy, "egress", 53) == {"kube-system"}
    assert public_egress_ports(litellm_policy) == {443}
    assert egress_ports_to(litellm_policy, DATABASE_SELECTOR) == set()
    assert policy_ports(litellm_policy, "ingress") == {("TCP", 4000)}
    assert policy_ports(litellm_policy, "egress") == {
        ("UDP", 53),
        ("TCP", 53),
        ("TCP", 3000),
        ("TCP", 443),
    }

    langfuse_policy = policy("langfuse-netpol")
    assert allowed_workloads(documents, langfuse_policy, "ingress", 3000) == {
        litellm,
        workers,
        api,
    }
    assert egress_ports_to(langfuse_policy, DATABASE_SELECTOR) == {5432}
    assert allowed_namespaces(langfuse_policy, "egress", 53) == {"kube-system"}
    assert public_egress_ports(langfuse_policy) == set()
    assert policy_ports(langfuse_policy, "ingress") == {("TCP", 3000)}
    assert policy_ports(langfuse_policy, "egress") == {
        ("UDP", 53),
        ("TCP", 53),
        ("TCP", 5432),
    }

    api_policy = policy("unified-api-ingress")
    assert allowed_workloads(documents, api_policy, "ingress", 8000) == {
        workload("caddy-tailscale"),
        workers,
    }
    assert allowed_workloads(documents, api_policy, "egress", 4000) == {litellm}
    assert allowed_workloads(documents, api_policy, "egress", 3000) == {langfuse}
    assert allowed_workloads(documents, api_policy, "egress", 7233) == {
        TEMPORAL_FRONTEND
    }
    assert egress_ports_to(api_policy, DATABASE_SELECTOR) == {5432}
    assert allowed_namespaces(api_policy, "egress", 53) == {"kube-system"}
    assert public_egress_ports(api_policy) == {443}
    assert policy_ports(api_policy, "ingress") == {("TCP", 8000)}

    workers_policy = policy("workers-netpol")
    assert workers_policy["spec"]["ingress"] == []
    assert allowed_workloads(documents, workers_policy, "egress", 3000) == {langfuse}
    assert allowed_workloads(documents, workers_policy, "egress", 4000) == {litellm}
    assert allowed_workloads(documents, workers_policy, "egress", 8000) == {api}
    assert allowed_workloads(documents, workers_policy, "egress", 7233) == {
        TEMPORAL_FRONTEND
    }
    assert allowed_namespaces(workers_policy, "egress", 50051) == {"agent-substrate"}
    assert allowed_namespaces(workers_policy, "egress", 53) == {"kube-system"}

    temporal_policy = policy("temporal-netpol")
    pods = pod_templates(documents)
    temporal_pods = {
        name
        for name, template in pods.items()
        if selector_matches(TEMPORAL_LABELS, template["metadata"]["labels"])
    }
    callers = {workers, api}
    from_7233 = allowed_workloads(documents, temporal_policy, "ingress", 7233)
    assert callers | {TEMPORAL_FRONTEND} <= from_7233
    assert from_7233 - callers <= temporal_pods
    for port in TEMPORAL_RING_PORTS:
        peers = allowed_workloads(documents, temporal_policy, "ingress", port)
        assert peers
        assert peers <= temporal_pods, port
        assert (
            allowed_workloads(documents, temporal_policy, "egress", port)
            <= temporal_pods
        )
    servers = {f"{RELEASE}-temporal-{role}" for role in TEMPORAL_ROLES}
    for name in servers:  # every rpc/membership containerPort must stay reachable
        (container,) = pods[name]["spec"]["containers"]
        for port in container["ports"]:
            if port["name"] in {"rpc", "membership"}:
                reachable = allowed_workloads(
                    documents, temporal_policy, "ingress", port["containerPort"]
                )
                assert servers <= reachable, (name, port)
    assert egress_ports_to(temporal_policy, DATABASE_SELECTOR) == {5432}
    assert allowed_namespaces(temporal_policy, "egress", 53) == {"kube-system"}
    assert public_egress_ports(temporal_policy) == set()
    # Exact surface: any extra port, protocol or peer kind widens the policy.
    ring = {("TCP", port) for port in (7233, *TEMPORAL_RING_PORTS)}
    dns_and_database = {("UDP", 53), ("TCP", 53), ("TCP", 5432)}
    assert policy_ports(temporal_policy, "ingress") == ring
    assert policy_ports(temporal_policy, "egress") == ring | dns_and_database
    assert peer_kinds(temporal_policy, "ingress") == {"podSelector"}
    assert peer_kinds(temporal_policy, "egress") == {
        "podSelector",
        "namespaceSelector",
    }


def test_temporal_dev_network_policy_contract(chart_dir: str) -> None:
    documents = render_profile(chart_dir, "kind")
    dev, api, workers = (
        workload(c) for c in ("temporal-dev", "unified-api", "workers")
    )
    dev_policy = resource(documents, "NetworkPolicy", workload("temporal-dev-netpol"))
    assert allowed_workloads(documents, dev_policy, "ingress", 7233) == {workers, api}
    assert dev_policy["spec"]["egress"] == []
    assert workload("temporal-netpol") not in names_of(documents, "NetworkPolicy")
    for component in ("unified-api-ingress", "workers-netpol"):
        policy = resource(documents, "NetworkPolicy", workload(component))
        assert allowed_workloads(documents, policy, "egress", 7233) == {dev}
    api_policy = resource(documents, "NetworkPolicy", workload("unified-api-ingress"))
    assert allowed_workloads(documents, api_policy, "ingress", 8000) == {workers}


def test_network_policies_omit_disabled_components(chart_dir: str) -> None:
    documents = render(
        Path(chart_dir), {"litellm.enabled": False, "langfuse.enabled": False}
    )
    names = names_of(documents, "NetworkPolicy")
    assert not {workload("litellm-netpol"), workload("langfuse-netpol")} & names
    api_policy = resource(documents, "NetworkPolicy", workload("unified-api-ingress"))
    for port in (4000, 3000):
        assert not rules_for_port(api_policy, "egress", port)


def test_runtime_environment_and_pull_policy_are_rendered(chart_dir: str) -> None:
    chart = Path(chart_dir)
    default = render(chart)
    kind = render(chart, values_files=[chart / "values-kind.yaml"])

    def container(documents: list[Manifest], component: str) -> Manifest:
        deployment = resource(documents, "Deployment", workload(component))
        return deployment["spec"]["template"]["spec"]["containers"][0]

    rustfs = container(default, "rustfs")
    assert environment(rustfs)["RUSTFS_OBS_LOG_DIRECTORY"] == ""
    langfuse = container(default, "langfuse")
    assert environment(langfuse)["HOME"] == "/tmp"
    assert "/tmp" in {mount["mountPath"] for mount in langfuse["volumeMounts"]}
    temporal_dev = container(kind, "temporal-dev")
    assert environment(temporal_dev)["USER"] == "temporal"
    assert environment(temporal_dev)["HOME"] in {
        mount["mountPath"] for mount in temporal_dev["volumeMounts"]
    }
    for component in ("unified-api", "frontend", "workers", "caddy-tailscale"):
        pod = resource(default, "Deployment", workload(component))["spec"]["template"][
            "spec"
        ]
        assert {c["imagePullPolicy"] for c in pod["containers"]} == {"Always"}, (
            component
        )


PLATFORM_PORTS = (
    "rustfs.service.port",
    "rustfs.consolePort",
    "unifiedApi.service.port",
    "frontend.service.port",
    "workers.health.port",
    "workers.substratePort",
    "litellm.service.port",
    "langfuse.service.port",
    "temporalDev.service.port",
    "temporal.server.frontend.service.port",
)
SUBSTRATE_PORTS = ("ateapi.service.port", "database.port", "rustfs.port")
BAD_PORTS = (0, 65536)
BAD_DNS_LABELS = ("Bad_Name", "-leading", "trailing-", "")


def rejected(chart: str, key: str, *values: Setting) -> list[Any]:
    return [
        pytest.param(chart, key, value, id=f"{chart}:{key}={str(value)[:24]}")
        for value in values
    ]


INVALID_VALUES: list[Any] = [
    *(c for port in PLATFORM_PORTS for c in rejected(PLATFORM, port, *BAD_PORTS)),
    *(c for port in SUBSTRATE_PORTS for c in rejected(SUBSTRATE, port, *BAD_PORTS)),
    *rejected(PLATFORM, "database.port", 5544),
    *rejected(PLATFORM, "workers.temporalNamespace", *BAD_DNS_LABELS, "a" * 47),
    *rejected(PLATFORM, "workers.substrateNamespace", *BAD_DNS_LABELS, "a" * 64),
    *rejected(PLATFORM, "rustfs.substrateNamespace", *BAD_DNS_LABELS, "a" * 64),
    *rejected(
        PLATFORM,
        "database.substrateUser",
        "postgres",
        "platform",
        "langfuse",
        "temporal",
        "admin",
        "pg_admin",
        "Upper",
        "1abc",
        "",
        "a" * 64,
    ),
    *rejected(
        PLATFORM,
        "database.substrateDatabase",
        "postgres",
        "template0",
        "template1",
        "platform",
        "langfuse",
        "temporal",
        "temporal_visibility",
        "Upper",
        "1abc",
        "",
        "a" * 64,
    ),
    *rejected(SUBSTRATE, "platformNamespace", *BAD_DNS_LABELS, "a" * 64),
    *rejected(SUBSTRATE, "workloadNamespace", *BAD_DNS_LABELS, "a" * 64),
    *rejected(
        PLATFORM,
        "temporal.server.config.namespaces.retention",
        "$(id)",
        # Temporal rejects a local-namespace retention below one hour.
        "30m",
        "0s",
        "500ms",
        "0h",
        "0d",
        "3 days",
        "",
        "abc",
    ),
]

ACCEPTED_BOUNDARY_VALUES: list[Any] = [
    pytest.param(PLATFORM, {p: 1 for p in PLATFORM_PORTS}, id="platform-ports-min"),
    pytest.param(PLATFORM, {p: 65535 for p in PLATFORM_PORTS}, id="platform-ports-max"),
    pytest.param(
        PLATFORM,
        {
            "workers.temporalNamespace": "a",
            "workers.substrateNamespace": "a",
            "rustfs.substrateNamespace": "a",
            "database.substrateUser": "a",
            "database.substrateDatabase": "a",
        },
        id="platform-single-character-names",
    ),
    pytest.param(
        PLATFORM,
        {
            "workers.temporalNamespace": "a" * 46,
            "workers.substrateNamespace": "a" * 63,
            "rustfs.substrateNamespace": "a" * 63,
            "database.substrateUser": "a" * 63,
            "database.substrateDatabase": "a" * 63,
        },
        id="platform-maximum-length-names",
    ),
    *(
        pytest.param(
            PLATFORM,
            {"temporal.server.config.namespaces.retention": retention},
            id=f"platform-retention-{retention}",
        )
        for retention in ("3d", "72h", "1h", "1d12h")
    ),
    pytest.param(SUBSTRATE, {p: 1 for p in SUBSTRATE_PORTS}, id="substrate-ports-min"),
    pytest.param(
        SUBSTRATE, {p: 65535 for p in SUBSTRATE_PORTS}, id="substrate-ports-max"
    ),
    pytest.param(
        SUBSTRATE,
        {"platformNamespace": "a", "workloadNamespace": "a"},
        id="substrate-single-character-namespaces",
    ),
    pytest.param(
        SUBSTRATE,
        {"platformNamespace": "a" * 63, "workloadNamespace": "a" * 63},
        id="substrate-maximum-length-namespaces",
    ),
]


@pytest.mark.parametrize("chart,key,value", INVALID_VALUES)
def test_values_schema_rejects_out_of_contract_values(
    chart_dir: str, chart: str, key: str, value: Setting
) -> None:
    stderr = render_error(chart_path(chart_dir, chart), {key: value})
    pointer = "/" + key.replace(".", "/")
    assert SCHEMA_ERROR in stderr
    assert f"at '{pointer}'" in stderr


@pytest.mark.parametrize("chart,settings", ACCEPTED_BOUNDARY_VALUES)
def test_values_schema_accepts_boundary_values(
    chart_dir: str, chart: str, settings: dict[str, Setting]
) -> None:
    render(chart_path(chart_dir, chart), settings)


@pytest.mark.parametrize(
    "key,value,pointer",
    [
        ("global.apiServerPorts", [], "/global/apiServerPorts"),
        ("global.apiServerPorts", [0], "/global/apiServerPorts/0"),
        ("global.apiServerPorts", [65536], "/global/apiServerPorts/0"),
        ("global.apiServerCIDRs", [""], "/global/apiServerCIDRs/0"),
    ],
)
def test_substrate_schema_rejects_invalid_api_server_lists(
    chart_dir: str, key: str, value: list[Any], pointer: str
) -> None:
    stderr = render_error(chart_path(chart_dir, SUBSTRATE), json_settings={key: value})
    assert SCHEMA_ERROR in stderr
    assert f"at '{pointer}'" in stderr


@pytest.mark.parametrize("ports", [[1], [65535], [1, 443, 65535]])
def test_substrate_schema_accepts_api_server_port_bounds(
    chart_dir: str, ports: list[int]
) -> None:
    render(
        chart_path(chart_dir, SUBSTRATE), json_settings={"global.apiServerPorts": ports}
    )


@pytest.mark.parametrize(
    "key,value",
    [
        (f"temporal.{group}.{key}", wrong)
        for group in ("server", "schema")
        for key, wrong in (
            ("nodeSelector", "notamap"),
            ("tolerations", "notalist"),
            ("affinity", "notamap"),
            ("podAnnotations", "notamap"),
        )
    ],
)
def test_temporal_scheduling_values_are_schema_validated(
    chart_dir: str, key: str, value: str
) -> None:
    stderr = render_error(Path(chart_dir), string_settings={key: value})
    assert SCHEMA_ERROR in stderr
    assert "/" + key.replace(".", "/") in stderr


@pytest.mark.parametrize(
    "chart,settings,namespace,expected",
    [
        (
            PLATFORM,
            {"temporal.enabled": True, "temporalDev.enabled": True},
            NAMESPACE,
            "mutually exclusive",
        ),
        (
            PLATFORM,
            {
                "temporal.enabled": False,
                "temporalDev.enabled": False,
                "workers.enabled": True,
            },
            NAMESPACE,
            "requires one Temporal profile",
        ),
        (
            SUBSTRATE,
            {},
            "ate-system",
            "must NOT be deployed into the 'ate-system' namespace",
        ),
        (
            SUBSTRATE,
            {"global.tls.useOcpServingCert": True},
            NAMESPACE,
            "OpenShift serving-cert mode requires tls.existingBundleSecret",
        ),
    ],
)
def test_cross_field_validation_rejects_invalid_profiles(
    chart_dir: str,
    chart: str,
    settings: dict[str, Setting],
    namespace: str,
    expected: str,
) -> None:
    stderr = render_error(chart_path(chart_dir, chart), settings, namespace=namespace)
    assert expected in stderr


@pytest.mark.parametrize(
    "chart,key",
    [
        (PLATFORM, "unknownKey"),
        (PLATFORM, "unifiedApi.unknownKey"),
        (PLATFORM, "temporal.unknownKey"),
        (SUBSTRATE, "unknownKey"),
        (SUBSTRATE, "ateapi.unknownKey"),
    ],
)
def test_values_schema_rejects_unknown_properties(
    chart_dir: str, chart: str, key: str
) -> None:
    stderr = render_error(chart_path(chart_dir, chart), {key: True})
    assert SCHEMA_ERROR in stderr
    assert "additional properties 'unknownKey' not allowed" in stderr


@pytest.mark.parametrize(
    "chart,key,value,expected",
    [
        (PLATFORM, "frontend.service.port", "3000", "got string, want integer"),
        (PLATFORM, "unifiedApi.enabled", "true", "got string, want boolean"),
        (SUBSTRATE, "ateapi.service.port", "50051", "got string, want integer"),
    ],
)
def test_values_schema_rejects_string_coerced_values(
    chart_dir: str, chart: str, key: str, value: str, expected: str
) -> None:
    stderr = render_error(chart_path(chart_dir, chart), string_settings={key: value})
    assert SCHEMA_ERROR in stderr
    assert expected in stderr


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
