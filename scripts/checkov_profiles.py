#!/usr/bin/env -S uv run
"""Scan raw non-deferred chart profiles and reject empty Checkov coverage."""

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from scripts.helm_dependencies import require_versions
from scripts.render_profiles import render_profiles

ROOT = Path(__file__).absolute().parent.parent
CHART_PROFILES = {
    "charts/platform": ("default", "kind", "bootstrap", "full"),
    "charts/agent-substrate": ("substrate",),
    "charts/postgres-operator": ("operator",),
}
OPERATOR_EXCEPTIONS = {
    "CKV_K8S_8": "Upstream 2.0.3 has no configurable operator liveness probe.",
    "CKV_K8S_29": "Upstream provides container, not pod, securityContext; the container is hardened.",
    "CKV_K8S_38": "This Kubernetes controller needs its bound ServiceAccount token.",
}


def selected_profiles(config: dict[str, Any]) -> list[str]:
    patterns = config.get("skip-path", [])
    if not isinstance(patterns, list) or any(
        not isinstance(path, str) for path in patterns
    ):
        raise TypeError("Checkov skip-path must be a list of source-path expressions")
    return [
        profile
        for source, profiles in CHART_PROFILES.items()
        if not any(re.search(pattern, source) for pattern in patterns)
        for profile in profiles
    ]


def verify_report(report: Any) -> dict[str, int]:
    if not isinstance(report, dict) or report.get("check_type") != "kubernetes":
        raise TypeError("Checkov must produce a Kubernetes report")
    raw = report.get("summary")
    if not isinstance(raw, dict):
        raise TypeError("Checkov must produce evaluation counts")
    summary = {
        key: int(raw.get(key, 0))
        for key in ("resource_count", "passed", "failed", "skipped", "parsing_errors")
    }
    if summary["resource_count"] <= 0 or summary["passed"] <= 0:
        raise ValueError("Checkov returned no evaluated resources/checks")
    return summary


def main() -> int:
    config = yaml.safe_load((ROOT / ".checkov.yaml").read_text())
    if not isinstance(config, dict):
        raise TypeError("Checkov configuration must be a mapping")
    profiles = selected_profiles(config)
    if not profiles:
        raise ValueError("No non-deferred Checkov chart profiles selected")
    cache = ROOT / ".cache/checkov"
    cache.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="profiles.", dir=cache))
    render_profiles(ROOT, output, "platform", "platform")
    failed = False
    for profile in profiles:
        command = [
            "checkov",
            "--framework",
            "kubernetes",
            "--config-file",
            str(ROOT / ".checkov.yaml"),
            "-f",
            str(output / f"{profile}.yaml"),
            "--output",
            "json",
            "--quiet",
        ]
        if profile == "operator":
            require_versions(
                [ROOT / "charts/postgres-operator"], ["postgres-operator=2.0.3"]
            )
            skips = [*config.get("skip-check", []), *OPERATOR_EXCEPTIONS]
            command.extend(["--skip-check", ",".join(skips)])
            print(f"operator 2.0.3 scoped exceptions: {OPERATOR_EXCEPTIONS}")
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, check=False
        )
        if result.returncode not in {0, 1}:
            raise RuntimeError(result.stderr or result.stdout)
        report = json.loads(result.stdout)
        summary = verify_report(report)
        print(f"{profile}: {summary}")
        for finding in report["results"]["failed_checks"]:
            print(
                f"{finding['check_id']}: {finding['resource']}: {finding['check_name']}"
            )
        failed |= (
            result.returncode != 0
            or summary.get("failed", 0) > 0
            or summary.get("parsing_errors", 0) > 0
        )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
