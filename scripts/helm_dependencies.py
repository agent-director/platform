#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Materialize chart-declared dependencies without updating committed locks."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).absolute().parent.parent
DEPENDENCY_FIELDS = (
    "name",
    "version",
    "repository",
    "condition",
    "tags",
    "enabled",
    "import-values",
    "alias",
)
EXACT_VERSION = re.compile(
    r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z"
)


def load_mapping(path: Path) -> dict[str, Any]:
    """Read trusted project metadata, rejecting a non-mapping YAML document."""
    with path.open() as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise TypeError(f"Expected a YAML mapping in {path}")
    return data


def _ordered_maps(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _ordered_maps(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_ordered_maps(item) for item in value]
    return value


def _helm_dependency(dependency: dict[str, Any]) -> dict[str, Any]:
    # Helm's chart.Dependency struct field order/omitempty rules, not YAML order.
    return {
        field: _ordered_maps(dependency.get(field, ""))
        for field in DEPENDENCY_FIELDS
        if field in {"name", "repository"} or dependency.get(field)
    }


def lock_digest(declared: list[dict[str, Any]], locked: list[dict[str, Any]]) -> str:
    """Match Helm resolver.HashReq's JSON encoding of [requirements, locks]."""
    payload = json.dumps(
        [
            [_helm_dependency(dep) for dep in declared],
            [_helm_dependency(dep) for dep in locked],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for character, escaped in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        (" ", "\\u2028"),
        (" ", "\\u2029"),
    ):
        payload = payload.replace(character, escaped)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def validate_lock(chart: Path) -> list[dict[str, Any]]:
    """Reject absent, drifted, or non-exact locks before any Helm mutation."""
    declared = load_mapping(chart / "Chart.yaml").get("dependencies", [])
    if not isinstance(declared, list) or any(
        not isinstance(dep, dict) for dep in declared
    ):
        raise ValueError(f"Invalid dependencies in {chart / 'Chart.yaml'}")
    lock_path = chart / "Chart.lock"
    if not declared:
        if lock_path.exists():
            raise ValueError(
                f"Dependency count drift in {lock_path}; remove the obsolete lock explicitly"
            )
        if any((chart / "charts").glob("*.tgz")) or any(
            (chart / "charts").glob("*/Chart.yaml")
        ):
            raise ValueError(f"Undeclared materialized subcharts in {chart / 'charts'}")
        return []
    if not lock_path.is_file():
        raise ValueError(
            f"Missing committed dependency lock: {lock_path}; update it explicitly"
        )
    lock = load_mapping(lock_path)
    locked = lock.get("dependencies")
    if not isinstance(locked, list) or len(declared) != len(locked):
        raise ValueError(f"Dependency count drift in {lock_path}")
    for requirement, dependency in zip(declared, locked, strict=True):
        if not isinstance(dependency, dict):
            raise TypeError(f"Invalid dependency in {lock_path}")
        for field in ("name", "repository", "version"):
            if requirement.get(field, "") != dependency.get(field, ""):
                raise ValueError(
                    f"Dependency {field} drift in {lock_path}; update the lock explicitly"
                )
        if not EXACT_VERSION.fullmatch(str(dependency.get("version", ""))):
            raise ValueError(
                f"Dependency must use an exact version in {chart / 'Chart.yaml'}"
            )
    if lock.get("digest") != lock_digest(declared, locked):
        raise ValueError(
            f"Dependency digest drift in {lock_path}; update the lock explicitly"
        )
    local = {
        (dependency["name"], dependency["version"])
        for dependency in locked
        if not dependency["repository"]
        or dependency["repository"].startswith("file://")
    }
    for metadata_path in sorted((chart / "charts").glob("*/Chart.yaml")):
        metadata = load_mapping(metadata_path)
        if (metadata.get("name"), str(metadata.get("version"))) not in local:
            raise ValueError(
                f"Undeclared unpacked subchart {metadata_path.parent}; normal builds do not remove directories"
            )
    return locked


def validate_materialized(chart: Path) -> None:
    """Reject missing/stale loaded subcharts after build and before rendering."""
    dependencies = validate_lock(chart)
    expected = {
        f"{dependency['name']}-{dependency['version']}.tgz"
        for dependency in dependencies
        if dependency["repository"]
    }
    actual = {path.name for path in (chart / "charts").glob("*.tgz")}
    if expected != actual:
        raise ValueError(
            f"Materialized dependency drift in {chart}: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    for dependency in dependencies:
        if not dependency["repository"]:
            continue
        artifact = (
            chart / "charts" / f"{dependency['name']}-{dependency['version']}.tgz"
        )
        result = subprocess.run(
            ["helm", "show", "chart", str(artifact)],
            check=True,
            capture_output=True,
            text=True,
        )
        metadata = yaml.safe_load(result.stdout)
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != dependency["name"]
            or str(metadata.get("version")) != dependency["version"]
        ):
            raise ValueError(
                f"Materialized artifact metadata does not match locked dependency: {artifact}"
            )
    unpacked = [load_mapping(path) for path in (chart / "charts").glob("*/Chart.yaml")]
    for dependency in dependencies:
        if not dependency["repository"] and not any(
            metadata.get("name") == dependency["name"]
            and str(metadata.get("version")) == dependency["version"]
            for metadata in unpacked
        ):
            raise ValueError(
                f"Missing declared local subchart {dependency['name']} in {chart}"
            )


def repository_registration(charts: list[Path]) -> list[tuple[str, str]]:
    urls: set[str] = set()
    for chart in charts:
        for dependency in validate_lock(chart):
            url = dependency.get("repository", "")
            if not isinstance(url, str):
                raise TypeError(f"Invalid repository in {chart / 'Chart.yaml'}")
            if url.startswith(("https://", "http://")):
                urls.add(url)
            elif url and not url.startswith(("file://", "oci://")):
                raise ValueError(f"Use an explicit repository URL, not an alias: {url}")
    return [
        ("project-" + hashlib.sha256(url.encode()).hexdigest()[:12], url)
        for url in sorted(urls)
    ]


def materialize(charts: list[Path], operation: str = "build") -> None:
    repositories = repository_registration(
        charts
    )  # Validate every lock before registering anything.
    if operation == "check":
        return
    for name, url in repositories:
        subprocess.run(["helm", "repo", "add", name, url, "--force-update"], check=True)
    if operation == "register":
        return
    for chart in charts:
        if not validate_lock(chart):
            continue
        lock_path = chart / "Chart.lock"
        original = lock_path.read_bytes()
        subprocess.run(
            ["helm", "dependency", "build", str(chart), "--skip-refresh"], check=True
        )
        if lock_path.read_bytes() != original:
            raise ValueError(f"Helm unexpectedly modified {lock_path}")
        validate_materialized(chart)


def require_versions(charts: list[Path], requirements: list[str]) -> None:
    """Guard version-specific consumer exceptions against a dependency upgrade."""
    dependencies = [
        dependency for chart in charts for dependency in validate_lock(chart)
    ]
    for requirement in requirements:
        name, separator, version = requirement.partition("=")
        if not separator or not name or not version:
            raise ValueError("Version guard must be NAME=EXACT_VERSION")
        matching = [
            dependency for dependency in dependencies if dependency.get("name") == name
        ]
        if not matching or any(
            dependency.get("version") != version for dependency in matching
        ):
            raise ValueError(
                f"Version-specific policy requires {requirement}; review exceptions before upgrading"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", nargs="?", choices=("build", "check", "register"), default="build"
    )
    parser.add_argument("charts", nargs="*", type=Path)
    parser.add_argument(
        "--require-version", action="append", default=[], metavar="NAME=VERSION"
    )
    args = parser.parse_args()
    charts = args.charts or [
        path.parent for path in sorted((ROOT / "charts").glob("*/Chart.yaml"))
    ]
    if not charts:
        parser.error("No charts found")
    try:
        require_versions(charts, args.require_version)
        materialize(charts, args.operation)
    except (
        OSError,
        ValueError,
        TypeError,
        subprocess.CalledProcessError,
        yaml.YAMLError,
    ) as error:
        parser.exit(1, f"Helm dependencies: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
