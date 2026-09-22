import subprocess
import pytest

NAMESPACE = "platform"


def check_pod_ready(label_selector: str):
    """Wait for a pod matching the label selector to be ready."""
    cmd = [
        "kubectl",
        "wait",
        "--for=condition=ready",
        "pod",
        "-l",
        label_selector,
        "-n",
        NAMESPACE,
        "--timeout=120s",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        pytest.fail(f"Pod matching {label_selector} not ready. Error: {e.stderr}")


def test_mcp_server_running():
    check_pod_ready("app=mcp-server")


def test_openshell_sandbox_running():
    check_pod_ready("app=openshell")


def test_postgres_running():
    check_pod_ready("app.kubernetes.io/name=postgresql")


def test_langfuse_running():
    check_pod_ready("app=langfuse")


def test_temporal_running():
    check_pod_ready("app.kubernetes.io/name=temporal")
