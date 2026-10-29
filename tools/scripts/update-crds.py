#!/usr/bin/env -S uv run
"""Update version-matched CRDs, kubeconform validation schemas, and substrate chart appVersion."""

import argparse
import copy
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
SUBSTRATE_VERSION = re.compile(
    r"^ARG AGENT_SUBSTRATE_VERSION=(v?\d+\.\d+\.\d+(?:-[\w.-]+)?)$", re.MULTILINE
)
POSTGRES_OPERATOR_VERSION = re.compile(
    r"^POSTGRES_OPERATOR_VERSION=(v?\d+\.\d+\.\d+(?:-[\w.-]+)?)$", re.MULTILINE
)
SUBSTRATE_CRD_NAME = re.compile(r"ate\.dev_[a-z0-9_.-]+\.yaml\Z")
CHART_APP_VERSION = re.compile(r'^appVersion: "[^"\n]*"$', re.MULTILINE)


def read_url(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "agent-director-crd-updater"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError(f"Response exceeds size limit: {url}")
    return payload


def to_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate OpenAPI v3 nullable nodes to JSON Schema Draft 4 anyOf."""
    converted = copy.deepcopy(schema)
    converted.pop("nullable", None)
    for keyword in ("properties", "patternProperties", "definitions", "$defs"):
        children = converted.get(keyword)
        if isinstance(children, dict):
            converted[keyword] = {
                name: to_json_schema(child) if isinstance(child, dict) else child
                for name, child in children.items()
            }
    for keyword in (
        "items",
        "additionalProperties",
        "additionalItems",
        "not",
        "allOf",
        "anyOf",
        "oneOf",
    ):
        child = converted.get(keyword)
        if isinstance(child, dict):
            converted[keyword] = to_json_schema(child)
        elif isinstance(child, list):
            converted[keyword] = [
                to_json_schema(item) if isinstance(item, dict) else item
                for item in child
            ]
    if schema.get("nullable") is True:
        return {"anyOf": [{"type": "null"}, converted]}
    return converted


def crd_outputs(
    content: bytes,
    group: str,
    crd_directory: Path | None,
    schema_directory: Path,
    filename: str,
) -> dict[Path, str]:
    outputs: dict[Path, str] = {}
    documents = list(yaml.safe_load_all(content))
    if not documents or any(not isinstance(document, dict) for document in documents):
        raise ValueError(f"Invalid CRD document in {filename}")
    for crd in documents:
        if (
            crd.get("kind") != "CustomResourceDefinition"
            or crd.get("apiVersion") != "apiextensions.k8s.io/v1"
        ):
            raise ValueError(f"Expected a v1 CustomResourceDefinition in {filename}")
        spec = crd.get("spec", {})
        if spec.get("group") != group:
            raise ValueError(f"Unexpected CRD group in {filename}")
        kind = spec.get("names", {}).get("kind")
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", kind):
            raise ValueError(f"Invalid CRD kind in {filename}")
        versions = spec.get("versions", [])
        if not versions:
            raise ValueError(f"Missing CRD versions in {filename}")
        for version in versions:
            if not version.get("served", False):
                continue
            version_name = version.get("name", "")
            if not isinstance(version_name, str) or not re.fullmatch(
                r"v[0-9]+(?:(?:alpha|beta)[0-9]+)?", version_name
            ):
                raise ValueError(f"Invalid served CRD version in {filename}")
            schema = copy.deepcopy(version.get("schema", {}).get("openAPIV3Schema"))
            if not isinstance(schema, dict):
                raise TypeError(
                    f"Missing or invalid served-version schema in {filename}"
                )
            properties = schema.setdefault("properties", {})
            properties["kind"] = {"type": "string", "enum": [kind]}
            properties["apiVersion"] = {
                "type": "string",
                "enum": [f"{group}/{version_name}"],
            }
            properties.setdefault("metadata", {"type": "object"})
            schema["required"] = list(
                dict.fromkeys([*schema.get("required", []), "kind", "apiVersion"])
            )
            path = schema_directory / f"{kind.lower()}_{version_name}.json"
            if path in outputs:
                raise ValueError(f"Duplicate served CRD schema: {path}")
            outputs[path] = json.dumps(to_json_schema(schema), indent=2) + "\n"
    if not outputs:
        raise ValueError(f"No served CRD schemas in {filename}")
    if crd_directory is not None:
        outputs[crd_directory / filename] = content.decode("utf-8").rstrip() + "\n"
    return outputs


def postgres_outputs(root: Path) -> dict[Path, str]:
    deploy_script = root / "tools/scripts/deploy.sh"
    match = POSTGRES_OPERATOR_VERSION.search(deploy_script.read_text())
    if match is None:
        raise ValueError(f"Missing exact POSTGRES_OPERATOR_VERSION in {deploy_script}")
    version = match.group(1)
    tag = version if version.startswith("v") else f"v{version}"
    url = (
        f"https://raw.githubusercontent.com/zalando/postgres-operator/{tag}/"
        "charts/postgres-operator/crds/postgresqls.yaml"
    )
    return crd_outputs(
        read_url(url),
        "acid.zalan.do",
        None,
        root / ".schemas/acid.zalan.do",
        "postgresqls.yaml",
    )


def substrate_outputs(root: Path) -> dict[Path, str]:
    dockerfile = root / "images/agent-substrate/Dockerfile"
    match = SUBSTRATE_VERSION.search(dockerfile.read_text())
    if match is None:
        raise ValueError(f"Missing exact AGENT_SUBSTRATE_VERSION in {dockerfile}")
    version = match.group(1)
    chart = root / "charts/agent-substrate/Chart.yaml"
    chart_metadata, count = CHART_APP_VERSION.subn(
        f'appVersion: "{version.removeprefix("v")}"', chart.read_text()
    )
    if count != 1:
        raise ValueError(f"Expected exactly one quoted appVersion in {chart}")
    api_url = (
        "https://api.github.com/repos/agent-substrate/substrate/contents/"
        f"manifests/ate-install/generated?ref={version}"
    )
    listing = json.loads(read_url(api_url))
    if not isinstance(listing, list):
        raise TypeError("Expected a substrate GitHub directory listing")
    names = sorted(
        {
            item["name"]
            for item in listing
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and SUBSTRATE_CRD_NAME.fullmatch(item["name"])
        }
    )
    if not names:
        raise ValueError(f"No substrate CRDs found at {version}")
    outputs: dict[Path, str] = {chart: chart_metadata}
    for filename in names:
        url = (
            f"https://raw.githubusercontent.com/agent-substrate/substrate/{version}/"
            f"manifests/ate-install/generated/{filename}"
        )
        generated = crd_outputs(
            read_url(url),
            "ate.dev",
            root / "charts/agent-substrate/crds",
            root / ".schemas/ate.dev",
            filename,
        )
        if outputs.keys() & generated.keys():
            raise ValueError(f"Duplicate substrate CRD output: {filename}")
        outputs.update(generated)
    return outputs


def olm_outputs(root: Path) -> dict[Path, str]:
    """Cache cluster-owned OLM API schemas; never install its CRDs through Helm."""
    metadata = yaml.safe_load((root / "charts/platform/Chart.yaml").read_text())
    if not isinstance(metadata, dict):
        raise TypeError("OLM schema source requires chart metadata mapping")
    revision = metadata.get("annotations", {}).get("agent-director.io/olm-schema-ref")
    if not isinstance(revision, str) or re.fullmatch(r"[a-f0-9]{40}", revision) is None:
        raise ValueError("OLM schemas require an immutable chart annotation source ref")
    directory = root / ".schemas/operators.coreos.com"
    sources = (
        ("0000_50_olm_00-operatorgroups.crd.yaml", "OperatorGroup", "v1"),
        ("0000_50_olm_00-subscriptions.crd.yaml", "Subscription", "v1alpha1"),
    )
    outputs: dict[Path, str] = {}
    for filename, kind, version in sources:
        url = (
            "https://raw.githubusercontent.com/openshift/operator-framework-olm/"
            f"{revision}/manifests/{filename}"
        )
        generated = crd_outputs(
            read_url(url),
            "operators.coreos.com",
            None,
            directory,
            filename,
        )
        path = directory / f"{kind.lower()}_{version}.json"
        if path not in generated:
            raise ValueError(f"Pinned OLM source does not serve {kind} {version}")
        schema = json.loads(generated[path])
        schema["$comment"] = f"OpenShift 4.22 OLM source: {url}"
        outputs[path] = json.dumps(schema, indent=2) + "\n"
    return outputs


def update(root: Path, operation: str) -> list[Path]:
    outputs: dict[Path, str] = {}
    if operation in {"postgres", "all"}:
        outputs.update(postgres_outputs(root))
    if operation in {"substrate", "all"}:
        outputs.update(substrate_outputs(root))
    if operation in {"olm", "all"}:
        outputs.update(olm_outputs(root))
    previous: set[Path] = set()
    if operation in {"postgres", "all"}:
        previous.update((root / ".schemas/acid.zalan.do").glob("*.json"))
    if operation in {"substrate", "all"}:
        crd_directory = root / "charts/agent-substrate/crds"
        for path in sorted(crd_directory.glob("*")):
            if path.is_file() and path.suffix in {".yaml", ".yml"}:
                previous.add(path)
        previous.update((root / ".schemas/ate.dev").glob("*.json"))
    if operation in {"olm", "all"}:
        previous.update((root / ".schemas/operators.coreos.com").glob("*.json"))
    for path, content in sorted(outputs.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for obsolete in sorted(previous - outputs.keys()):
        obsolete.unlink()
    return sorted(outputs.keys() | previous)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        nargs="?",
        choices=("postgres", "substrate", "olm", "all"),
        default="all",
    )
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="Repository root (fixture/checkouts)"
    )
    args = parser.parse_args()
    try:
        for path in update(args.root, args.operation):
            print(path.relative_to(args.root))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        yaml.YAMLError,
    ) as error:
        parser.exit(1, f"CRD update: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
