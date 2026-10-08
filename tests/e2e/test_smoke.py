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


@pytest.mark.parametrize(
    "label_selector",
    [
        "application=spilo",
        "app=langfuse",
        "app.kubernetes.io/name=temporal",
        "app=unified-api",
        "app=litellm",
        "app=rustfs",
        "app=workers",
    ],
)
def test_core_platform_components_ready(label_selector: str):
    check_pod_ready(label_selector)


def test_tailscale_components_ready():
    if os.environ.get("TEST_TAILSCALE", "false") == "true":
        check_pod_ready("app=tailscale-ingress")


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
