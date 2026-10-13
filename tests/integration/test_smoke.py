import os
import re
import socket
import ssl
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import (
    IntegrationEnvironment,
    parse_resources,
    required_value,
    run_tool,
    secret_data,
    substrate_service,
)


def test_api_health_via_owned_forward(api_url: str) -> None:
    response = httpx.get(f"{api_url}/health", timeout=5.0)
    assert response.status_code == 200


def test_frontend_serves_page_and_built_assets(
    integration_env: IntegrationEnvironment,
) -> None:
    """Exercise standalone SSR and asset packaging, not merely server startup."""
    contract = integration_env.contract
    services = [
        resource
        for resource in contract.manifest(contract.release, contract.namespace)
        if resource["kind"] == "Service"
        and resource.get("spec", {}).get("selector", {}).get("app") == "frontend"
    ]
    assert len(services) == 1, "Expected one chart-owned frontend Service"
    service = services[0]
    port = next(
        port["port"] for port in service["spec"]["ports"] if port["name"] == "http"
    )
    with integration_env.forward_manager.scoped_forward(
        contract.namespace, service["metadata"]["name"], str(port)
    ) as local_port:
        url = f"http://127.0.0.1:{local_port}"
        response = httpx.get(url, timeout=10)
        assert response.status_code == 200
        assert "Workflows" in response.text
        assets = re.findall(r'<script[^>]+src="([^"]+)"', response.text)
        assert assets, "Expected built JavaScript assets in frontend HTML"
        for asset in assets:
            assert asset.startswith("/_next/")
            script = httpx.get(f"{url}{asset}", timeout=10)
            assert script.status_code == 200
            assert "javascript" in script.headers["content-type"]


def test_every_rendered_workload_is_ready(
    integration_env: IntegrationEnvironment,
) -> None:
    """Discover all installed platform/provider/substrate workloads; never reduce coverage on errors."""
    contract = integration_env.contract
    for workload in integration_env.workloads:
        contract.kubectl(
            "rollout",
            "status",
            f"{workload.kind.lower()}/{workload.name}",
            "--namespace",
            workload.namespace,
            "--timeout=120s",
        )
        contract.kubectl(
            "wait",
            "--for=condition=Ready",
            "pod",
            "--selector",
            workload.selector,
            "--namespace",
            workload.namespace,
            "--timeout=120s",
        )
    # Spilo is created by the provider rather than appearing in Helm's manifest.
    contract.kubectl(
        "wait",
        "--for=condition=Ready",
        "pod",
        "--selector",
        f"cluster-name={required_value(integration_env.lifecycle, 'databaseResource')}",
        "--namespace",
        contract.namespace,
        "--timeout=120s",
    )


@pytest.mark.parametrize("client_certificate", [False, True])
def test_substrate_grpc_listener_presents_valid_tls(
    integration_env: IntegrationEnvironment, tmp_path: Path, client_certificate: bool
) -> None:
    """Verify server identity and transport with/without the controller's client cert."""
    endpoint = integration_env.substrate
    context = ssl.create_default_context(cafile=str(endpoint.ca_file))
    context.set_alpn_protocols(["h2"])
    if client_certificate:
        contract = integration_env.contract
        _, _, name = substrate_service(
            contract.manifest(contract.substrate_release, contract.substrate_namespace)
        )
        bundle = tmp_path / "client-bundle.pem"
        bundle.write_text(
            secret_data(contract, contract.substrate_namespace, name)["bundle.pem"]
        )
        bundle.chmod(0o600)
        context.load_cert_chain(str(bundle))
    with (
        socket.create_connection((endpoint.address, endpoint.port), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=endpoint.server_name) as connection,
    ):
        assert connection.getpeercert()
        assert connection.selected_alpn_protocol() == "h2"
        # TLS 1.3 can complete client-side before server client-cert verification.
        # A real HTTP/2 SETTINGS response proves the server accepted the transport.
        connection.sendall(
            b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
            + b"\x00\x00\x00\x04\x00\x00\x00\x00\x00"
        )
        header = b""
        while len(header) < 9:
            part = connection.recv(9 - len(header))
            assert part, "Substrate closed transport before its HTTP/2 SETTINGS"
            header += part
        assert header[3] == 4


def test_substrate_database_password_stays_out_of_flags_and_logs(
    integration_env: IntegrationEnvironment,
) -> None:
    contract = integration_env.contract
    apis = [
        resource
        for resource in contract.manifest(
            contract.substrate_release, contract.substrate_namespace
        )
        if resource["kind"] == "Deployment"
        and resource["spec"]["selector"]["matchLabels"].get(
            "app.kubernetes.io/component"
        )
        == "ateapi"
    ]
    assert len(apis) == 1
    api = apis[0]
    container = api["spec"]["template"]["spec"]["containers"][0]
    settings = {entry["name"]: entry for entry in container["env"]}
    assert "$(PGPASSWORD)" not in settings["POSTGRES_URL"]["value"]
    assert "$(PGPASSWORD)" not in " ".join(container["args"])
    reference = settings["PGPASSWORD"]["valueFrom"]["secretKeyRef"]
    password = secret_data(contract, contract.substrate_namespace, reference["name"])[
        reference["key"]
    ]
    logs = contract.kubectl(
        "logs",
        f"deployment/{api['metadata']['name']}",
        "--namespace",
        contract.substrate_namespace,
    ).stdout
    if password in logs:
        pytest.fail("Scoped database password is present in substrate logs")
    if "Final flag values" not in logs:
        pytest.fail("Substrate startup flag log is missing")
    if "PostgreSQL migrations ready" not in logs:
        pytest.fail("Substrate database migration readiness log is missing")


def test_rendered_caddy_config_with_built_plugin_image(
    integration_env: IntegrationEnvironment, chart_dir: str, tmp_path: Path
) -> None:
    """Validate with the repository's built plugin image; no stock-image fallback or silent skip."""
    image = os.environ.get("CADDY_IMAGE")
    if not image:
        raise ValueError("CADDY_IMAGE must identify the locally built plugin image")
    contract = integration_env.contract
    rendered = run_tool(
        "helm",
        "template",
        contract.release,
        chart_dir,
        "--namespace",
        contract.namespace,
        "-f",
        str(Path(chart_dir) / "values-kind.yaml"),
        "--set",
        "caddyTailscale.enabled=true",
        "--set",
        "caddyTailscale.tailnet=integration.invalid",
    )
    resources = parse_resources(rendered.stdout)
    configs = [
        resource["data"]["Caddyfile"]
        for resource in resources
        if resource["kind"] == "ConfigMap" and "Caddyfile" in resource.get("data", {})
    ]
    assert len(configs) == 1, "Caddy ConfigMap discovery failed"
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(configs[0], encoding="utf-8")
    caddyfile.chmod(0o644)
    run_tool(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--env",
        "XDG_CONFIG_HOME=/tmp/config",
        "--env",
        "XDG_DATA_HOME=/tmp/data",
        "--volume",
        f"{caddyfile}:/etc/caddy/Caddyfile:ro",
        "--entrypoint",
        "/usr/bin/caddy",
        image,
        "validate",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
    )
