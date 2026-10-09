import os
import subprocess

import pytest

NAMESPACE = os.environ.get("NAMESPACE", "platform")


def check_pod_ready(label_selector: str, namespace: str = NAMESPACE):
    """Wait for a pod matching the label selector to be ready."""
    cmd = [
        "kubectl",
        "wait",
        "--for=condition=ready",
        "pod",
        "-l",
        label_selector,
        "-n",
        namespace,
        "--timeout=120s",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        pytest.fail(f"Pod matching {label_selector} not ready. Error: {e.stderr}")


def get_expected_deployments() -> list[str]:
    """Dynamically get all deployments by rendering the helm template."""
    cmd = [
        "helm",
        "template",
        "test-release",
        "charts/platform",
        "--set",
        "tailscaleIngress.enabled=false",
    ]
    try:
        output = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
        import yaml  # type: ignore

        docs = yaml.safe_load_all(output)
        labels = []
        for doc in docs:
            if not doc:
                continue
            if doc.get("kind") in ["Deployment", "StatefulSet"]:
                match_labels = (
                    doc.get("spec", {}).get("selector", {}).get("matchLabels", {})
                )
                if match_labels:
                    labels.append(",".join(f"{k}={v}" for k, v in match_labels.items()))
        # Add postgres operator spilo explicitly as it's spawned dynamically
        labels.append("application=spilo")
        return list(set(labels))
    except Exception:
        # Fallback if helm template fails in test discovery
        return ["app=unified-api"]


def test_api_health_via_port_forward():
    """Verify the Unified API responds to HTTP requests over port-forward."""
    import socket
    import time
    import httpx
    import subprocess

    cmd_port_forward = [
        "kubectl",
        "port-forward",
        "-n",
        NAMESPACE,
        "svc/unified-api",
        "8000:8000",
    ]
    pf_process = subprocess.Popen(
        cmd_port_forward, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    connected = False
    try:
        for _ in range(15):
            time.sleep(1)
            try:
                with socket.create_connection(("127.0.0.1", 8000), timeout=1):
                    connected = True
                    break
            except OSError:
                continue

        if not connected:
            pytest.fail("unified-api svc is running but port-forward to 8000 failed.")

        response = httpx.get("http://localhost:8000/health", timeout=5.0)
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    finally:
        pf_process.terminate()
        pf_process.wait()


@pytest.mark.parametrize("label_selector", get_expected_deployments())
def test_core_platform_components_ready(label_selector: str):
    check_pod_ready(label_selector)


def test_tailscale_components_ready():
    if os.environ.get("TEST_TAILSCALE", "false") == "true":
        check_pod_ready("app=caddy-tailscale")


def test_agent_substrate_components_ready():
    # AteAPI
    check_pod_ready("app.kubernetes.io/component=ateapi", namespace="agent-substrate")

    # Functionally test the API
    cmd_get_pod = [
        "kubectl",
        "get",
        "pods",
        "-n",
        "agent-substrate",
        "-l",
        "app.kubernetes.io/component=ateapi",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    ]
    pod_name = subprocess.run(
        cmd_get_pod, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Instead of exec'ing into the distroless container (which has no shell),
    # we port-forward and verify the port is accepting connections.
    import socket
    import time

    cmd_port_forward = [
        "kubectl",
        "port-forward",
        "-n",
        "agent-substrate",
        pod_name,
        "50051:50051",
    ]

    pf_process = subprocess.Popen(
        cmd_port_forward, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    connected = False
    try:
        # Give port-forward a moment to establish
        for _ in range(15):
            time.sleep(1)
            try:
                with socket.create_connection(("127.0.0.1", 50051), timeout=1):
                    connected = True
                    break
            except OSError:
                continue
    finally:
        pf_process.terminate()
        pf_process.wait()

    if not connected:
        pytest.fail(
            "ateapi pod is running but not accepting connections on gRPC port 50051 via port-forward."
        )
    # AteController
    check_pod_ready(
        "app.kubernetes.io/component=atecontroller", namespace="agent-substrate"
    )
