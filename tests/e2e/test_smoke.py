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


def test_core_platform_components_ready():
    # Zalando Postgres cluster
    check_pod_ready("application=spilo")

    # LiteLLM Proxy
    check_pod_ready("app=litellm")

    # Tailscale Ingress
    check_pod_ready("app=tailscale-ingress")

    # Minio
    check_pod_ready("app=minio")


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

    cmd_check_port = [
        "kubectl",
        "exec",
        "-n",
        "agent-substrate",
        pod_name,
        "--",
        "bash",
        "-c",
        "cat /proc/net/tcp | grep -q ':C383'",
    ]
    try:
        subprocess.run(cmd_check_port, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        pytest.fail(
            f"ateapi pod is running but not listening on gRPC port 50051. Error: {e.stderr}"
        )

    # AteController
    check_pod_ready(
        "app.kubernetes.io/component=atecontroller", namespace="agent-substrate"
    )
