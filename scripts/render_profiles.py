#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Render actual chart profiles for offline gates; never deploy or rewrite resources."""

import argparse
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

try:
    from scripts.helm_dependencies import (
        require_versions,
        validate_lock,
        validate_materialized,
    )
except ModuleNotFoundError:
    from helm_dependencies import require_versions, validate_lock, validate_materialized

ROOT = Path(__file__).absolute().parent.parent
PROFILE_FILES = (
    "default.yaml",
    "kind.yaml",
    "bootstrap.yaml",
    "full.yaml",
    "substrate.yaml",
    "operator.yaml",
)


def _private_opener(name: str, flags: int) -> int:
    return os.open(name, flags, 0o600)


def write_private(path: Path, content: str) -> None:
    with open(path, "x", opener=_private_opener) as stream:
        stream.write(content)


def helm_template(release: str, chart: Path, namespace: str, options: list[str]) -> str:
    result = subprocess.run(
        ["helm", "template", release, str(chart), "--namespace", namespace, *options],
        check=True,
        capture_output=True,
        text=True,
    )
    documents = [
        document
        for document in yaml.safe_load_all(result.stdout)
        if isinstance(document, dict)
    ]
    if not documents:
        raise ValueError(f"Helm rendered no Kubernetes resources for {chart}")
    return result.stdout


def lifecycle_data(rendered: str) -> dict[str, Any]:
    contracts = [
        document
        for document in yaml.safe_load_all(rendered)
        if isinstance(document, dict)
        and document.get("kind") == "ConfigMap"
        and document.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "lifecycle-contract"
    ]
    if len(contracts) != 1 or not isinstance(contracts[0].get("data"), dict):
        raise ValueError("Expected exactly one chart-derived lifecycle contract")
    return contracts[0]["data"]


def render_profiles(
    root: Path, output: Path, release: str = "platform", namespace: str = "platform"
) -> None:
    platform = root / "charts/platform"
    substrate = root / "charts/agent-substrate"
    operator = root / "charts/postgres-operator"
    for chart in [platform, substrate, operator]:
        validate_lock(chart)
    require_versions([operator], ["postgres-operator=2.0.3"])
    for chart in [platform, substrate, operator]:
        validate_materialized(chart)
    # Rendering is offline: these are fixture values/references, never credentials
    # from a cluster or .env. Raw Helm output may include ephemeral generated TLS
    # material, so every output file remains private inside repository .cache.
    common = [
        "--set-string",
        "secrets.langfuseNextauthSecret=local-validation-only",
        "--set-string",
        "secrets.langfuseSalt=local-validation-only",
    ]
    bootstrap_options = [
        *common,
        "--set",
        "bootstrap.enabled=true",
        "--set",
        "temporal.enabled=false",
        "--set",
        "temporalDev.enabled=false",
    ]
    bootstrap = helm_template(release, platform, namespace, bootstrap_options)
    contract = lifecycle_data(bootstrap)
    projection = contract.get("temporal-values.yaml")
    if projection is None:
        raise ValueError("Missing chart-derived Temporal values projection")
    if not isinstance(projection, str) or not isinstance(
        yaml.safe_load(projection), dict
    ):
        raise TypeError("Invalid chart-derived Temporal values projection")
    projection_path = output / "temporal-values.yaml"
    write_private(projection_path, projection)
    rendered = {
        "default.yaml": helm_template(release, platform, namespace, common),
        "kind.yaml": helm_template(
            release,
            platform,
            namespace,
            [*common, "-f", str(platform / "values-kind.yaml")],
        ),
        "bootstrap.yaml": bootstrap,
        "full.yaml": helm_template(
            release,
            platform,
            namespace,
            [
                *common,
                "--is-upgrade",
                "-f",
                str(projection_path),
                "--set",
                "temporal.enabled=true",
                "--set",
                "temporalDev.enabled=false",
                "--set",
                "workers.enabled=true",
                "--set",
                "bootstrap.prerequisitesReady=true",
            ],
        ),
        "substrate.yaml": helm_template(
            "agent-substrate", substrate, "agent-substrate", []
        ),
        "operator.yaml": helm_template("postgres-operator", operator, namespace, []),
    }
    for name in PROFILE_FILES:
        write_private(output / name, rendered[name])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, help="New directory under the repository .cache"
    )
    parser.add_argument("--release", default="platform")
    parser.add_argument("--namespace", default="platform")
    args = parser.parse_args()
    cache = ROOT / ".cache/rendered"
    cache.mkdir(parents=True, exist_ok=True)
    try:
        if args.output_dir is None:
            output = Path(tempfile.mkdtemp(prefix="profiles.", dir=cache))
        else:
            output = args.output_dir.absolute()
            if ".." in output.parts or not output.is_relative_to(ROOT / ".cache"):
                raise ValueError("Render outputs must stay inside repository .cache")
            output.mkdir(parents=True, mode=0o700)
        render_profiles(ROOT, output, args.release, args.namespace)
    except (
        OSError,
        ValueError,
        TypeError,
        subprocess.CalledProcessError,
        yaml.YAMLError,
    ) as error:
        detail = (
            error.stderr
            if isinstance(error, subprocess.CalledProcessError)
            else str(error)
        )
        parser.exit(1, f"Chart profiles: {detail or error}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
