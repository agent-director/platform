import os
import subprocess
import pytest

NAMESPACE = os.environ.get("NAMESPACE", "platform")


def check_pod_ready(label_selector: str):
    """Wait for a pod matching the label selector to be ready."""
    try:
        subprocess.run(
            [
                "kubectl",
                "wait",
                "--for=condition=ready",
                "pod",
                "-l",
                label_selector,
                "-n",
                NAMESPACE,
                "--timeout=60s",
            ],
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def test_core_platform_components_ready():
    # Zaland Postgres cluster
    assert check_pod_ready("application=spilo")

    # LiteLLM Proxy
    assert check_pod_ready("app=litellm")

    # Tailscale Ingress
    assert check_pod_ready("app=tailscale-ingress")


def test_agent_substrate_components_ready():
    # AteAPI
    assert check_pod_ready("app.kubernetes.io/component=ateapi")
    
    # AteController
    assert check_pod_ready("app.kubernetes.io/component=atecontroller")
    
    # Atelet DaemonSet
    assert check_pod_ready("app.kubernetes.io/component=atelet")
