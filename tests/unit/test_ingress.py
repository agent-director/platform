"""Pure Helm ingress contracts; plugin/container validation belongs to integration."""

import subprocess
from collections.abc import Mapping
from typing import Any

import pytest
import yaml

Manifest = dict[str, Any]
EMPTY_ITEMS: tuple[object, ...] = ()
EMPTY_MAP: Mapping[str, object] = {}
RELEASE_NAME = "test-release-platform"


def render_chart(
    chart_dir: str, values: Mapping[str, str | int | bool] | None = None
) -> list[Manifest]:
    cmd = [
        "helm",
        "template",
        "test-release",
        chart_dir,
        "--namespace",
        "ingress-tests",
    ]
    for key, value in (values or {}).items():
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        cmd.extend(["--set", f"{key}={rendered}"])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


@pytest.fixture(scope="module")
def manifests(chart_dir: str) -> list[Manifest]:
    return render_chart(chart_dir, {"caddyTailscale.tailnet": "test.ts.net"})


def find_manifest(manifests: list[Manifest], kind: str, name: str) -> Manifest | None:
    return next(
        (
            document
            for document in manifests
            if document.get("kind") == kind
            and document.get("metadata", EMPTY_MAP).get("name") == name
        ),
        None,
    )


def get_egress_ports(manifest: Manifest) -> list[int]:
    return [
        port["port"]
        for rule in manifest["spec"].get("egress", EMPTY_ITEMS)
        for port in rule.get("ports", EMPTY_ITEMS)
        if "port" in port
    ]


def test_tailscale_deployment(manifests: list[Manifest]) -> None:
    deployment = find_manifest(
        manifests, "Deployment", f"{RELEASE_NAME}-caddy-tailscale"
    )
    assert deployment is not None
    spec = deployment["spec"]["template"]["spec"]
    assert not spec.get("initContainers")
    assert spec["automountServiceAccountToken"] is False
    caddy = next(
        container for container in spec["containers"] if container["name"] == "caddy"
    )
    assert "TS_KUBE_SECRET" not in {variable["name"] for variable in caddy["env"]}
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert caddy["securityContext"]["readOnlyRootFilesystem"] is True
    assert "ALL" in caddy["securityContext"]["capabilities"]["drop"]
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert deployment["spec"]["replicas"] == 1
    assert caddy["readinessProbe"]["httpGet"] == {"path": "/health", "port": "health"}


def test_caddyfile_structural_contract(manifests: list[Manifest]) -> None:
    """Assert the rendered contract, not binary/plugin compatibility or authentication."""
    config = find_manifest(manifests, "ConfigMap", f"{RELEASE_NAME}-caddy-config")
    assert config is not None
    caddyfile = config["data"]["Caddyfile"]
    assert caddyfile.lstrip().startswith("{")
    for directive in (
        "tailscale {",
        "ephemeral false",
        "read_body 120s",
        "read_header 5s",
        "trusted_proxies static 127.0.0.1/8 ::1/128",
        "bind tailscale/",
        "tailscale_auth",
        "request_header Tailscale-User-Login {http.auth.user.tailscale_user}",
        "request_header X-Tailscale-Tailnet {http.auth.user.tailscale_tailnet}",
        "max_size 10MB",
        "@api path /api /api/* /v1 /v1/*",
        "handle @api {",
        "reverse_proxy test-release-platform-unified-api.ingress-tests.svc.cluster.local:8000",
        "reverse_proxy test-release-platform-frontend.ingress-tests.svc.cluster.local:3000",
        ":8080 {",
        "respond /health 200",
    ):
        assert directive in caddyfile
    assert "handle_path" not in caddyfile, "API paths must not be stripped"
    assert "X-Webauth-User" not in caddyfile
    assert "{http.request.header.Tailscale-User-Login}" not in caddyfile
    assert caddyfile.index("handle @api") < caddyfile.index(
        "reverse_proxy test-release-platform-frontend"
    )


def test_ingress_port_overrides(chart_dir: str) -> None:
    rendered = render_chart(
        chart_dir,
        {"frontend.service.port": 3339, "unifiedApi.service.port": 8189},
    )
    config = find_manifest(rendered, "ConfigMap", f"{RELEASE_NAME}-caddy-config")
    assert config is not None
    assert (
        "test-release-platform-frontend.ingress-tests.svc.cluster.local:3339"
        in config["data"]["Caddyfile"]
    )
    assert (
        "test-release-platform-unified-api.ingress-tests.svc.cluster.local:8189"
        in config["data"]["Caddyfile"]
    )
    for component, port in (("frontend", 3339), ("unified-api", 8189)):
        deployment = find_manifest(
            rendered, "Deployment", f"{RELEASE_NAME}-{component}"
        )
        service = find_manifest(rendered, "Service", f"{RELEASE_NAME}-{component}")
        assert deployment is not None and service is not None
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["ports"][0]["containerPort"] == port
        assert service["spec"]["ports"][0]["port"] == port
        assert service["spec"]["ports"][0]["targetPort"] == "http"
        policy = find_manifest(
            rendered, "NetworkPolicy", f"{RELEASE_NAME}-{component}-ingress"
        )
        assert policy is not None
        assert policy["spec"]["ingress"][0]["ports"] == [
            {"protocol": "TCP", "port": port}
        ]
        assert policy["spec"]["ingress"][0]["from"][0]["podSelector"][
            "matchLabels"
        ] == {"app": "caddy-tailscale", "app.kubernetes.io/instance": "test-release"}
    egress = find_manifest(
        rendered, "NetworkPolicy", f"{RELEASE_NAME}-tailscale-egress"
    )
    assert egress is not None
    for component, port in (("frontend", 3339), ("unified-api", 8189)):
        rule = next(
            rule
            for rule in egress["spec"]["egress"]
            if any(
                peer.get("podSelector", EMPTY_MAP)
                .get("matchLabels", EMPTY_MAP)
                .get("app")
                == component
                for peer in rule.get("to", EMPTY_ITEMS)
            )
        )
        assert rule["ports"] == [{"protocol": "TCP", "port": port}]
        assert (
            rule["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/instance"]
            == "test-release"
        )


def test_network_policies(manifests: list[Manifest]) -> None:
    ingress = find_manifest(
        manifests, "NetworkPolicy", f"{RELEASE_NAME}-frontend-ingress"
    )
    assert ingress is not None
    labels = ingress["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
    assert labels == {
        "app": "caddy-tailscale",
        "app.kubernetes.io/instance": "test-release",
    }
    egress = find_manifest(
        manifests, "NetworkPolicy", f"{RELEASE_NAME}-tailscale-egress"
    )
    assert egress is not None
    assert egress["spec"]["ingress"] == []
    assert set(egress["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert {53, 443, 3478, 3000, 8000} <= set(get_egress_ports(egress))
    for rule in egress["spec"]["egress"]:
        for destination in rule.get("to", EMPTY_ITEMS):
            if "ipBlock" in destination:
                assert destination["ipBlock"]["cidr"] == "0.0.0.0/0"
                assert {
                    "10.0.0.0/8",
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                    "169.254.0.0/16",
                } <= set(destination["ipBlock"]["except"])
    api = find_manifest(
        manifests, "NetworkPolicy", f"{RELEASE_NAME}-unified-api-ingress"
    )
    assert api is not None
    callers = api["spec"]["ingress"][0]["from"]
    assert all(
        caller["podSelector"]["matchLabels"]
        == {"app": "caddy-tailscale", "app.kubernetes.io/instance": "test-release"}
        for caller in callers
    )


def test_tailscale_disabled(chart_dir: str) -> None:
    rendered = render_chart(chart_dir, {"caddyTailscale.enabled": False})
    for kind, suffix in (
        ("Deployment", "caddy-tailscale"),
        ("ConfigMap", "caddy-config"),
        ("NetworkPolicy", "frontend-ingress"),
        ("NetworkPolicy", "tailscale-egress"),
    ):
        assert find_manifest(rendered, kind, f"{RELEASE_NAME}-{suffix}") is None


@pytest.mark.parametrize("component,port", [("frontend", 3000), ("unifiedApi", 8000)])
def test_disabled_backend_has_no_caddy_egress(
    chart_dir: str, component: str, port: int
) -> None:
    rendered = render_chart(chart_dir, {f"{component}.enabled": False})
    egress = find_manifest(
        rendered, "NetworkPolicy", f"{RELEASE_NAME}-tailscale-egress"
    )
    assert egress is not None
    assert port not in get_egress_ports(egress)
    config = find_manifest(rendered, "ConfigMap", f"{RELEASE_NAME}-caddy-config")
    assert config is not None
    assert f":{port}" not in config["data"]["Caddyfile"]
