import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.checkov_profiles import (
    OPERATOR_EXCEPTIONS,
    selected_profiles,
    verify_report,
)

ROOT = Path(__file__).absolute().parents[2]


def test_checkov_preserves_declared_platform_deferral() -> None:
    config = yaml.safe_load((ROOT / ".checkov.yaml").read_text())
    assert config["skip-path"] == ["charts/platform"]
    assert selected_profiles(config) == ["substrate", "operator"]
    assert selected_profiles({}) == [
        "default",
        "kind",
        "bootstrap",
        "full",
        "substrate",
        "operator",
    ]
    assert set(OPERATOR_EXCEPTIONS) == {"CKV_K8S_8", "CKV_K8S_29", "CKV_K8S_38"}


@pytest.mark.parametrize(
    "report",
    [
        None,
        {"check_type": "helm", "summary": {"resource_count": 1, "passed": 1}},
        {"check_type": "kubernetes", "summary": {"resource_count": 0, "passed": 0}},
        {"check_type": "kubernetes", "summary": {"resource_count": 1, "passed": 0}},
    ],
)
def test_empty_or_wrong_checkov_evaluation_cannot_pass(report: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        verify_report(report)


def test_checkov_native_liveness_positive_control(tmp_path: Path) -> None:
    path = tmp_path / "pod.yaml"
    container: dict[str, Any] = {
        "name": "app",
        "image": "busybox:1.37.0",
        "livenessProbe": {"exec": {"command": ["/bin/true"]}},
    }
    document = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "checkov-control", "namespace": "fixture"},
        "spec": {"containers": [container]},
    }
    command = [
        str(ROOT / "scripts/mise.sh"),
        "exec",
        "--",
        "prek",
        "exec",
        "checkov",
        "--",
        "checkov",
        "--framework",
        "kubernetes",
        "--config-file",
        str(ROOT / ".checkov.yaml"),
        "--check",
        "CKV_K8S_8",
        "-f",
        str(path),
        "--output",
        "json",
        "--quiet",
    ]
    path.write_text(yaml.safe_dump(document))
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert verify_report(json.loads(result.stdout))["resource_count"] == 1
    container.pop("livenessProbe")
    path.write_text(yaml.safe_dump(document))
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["summary"]["failed"] == 1
    assert report["results"]["failed_checks"][0]["check_id"] == "CKV_K8S_8"
