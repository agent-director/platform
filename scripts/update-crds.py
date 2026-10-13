#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Update only the selected provider's version-matched CRDs and validation schemas."""

import argparse
import copy
import gzip
import io
import json
import re
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

try:
    from scripts.helm_dependencies import validate_lock
except ModuleNotFoundError:
    from helm_dependencies import validate_lock

ROOT = Path(__file__).absolute().parent.parent
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 2048
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
SUBSTRATE_VERSION = re.compile(
    r"^ARG AGENT_SUBSTRATE_VERSION=(v?\d+\.\d+\.\d+(?:-[\w.-]+)?)$", re.MULTILINE
)
SUBSTRATE_CRD_NAME = re.compile(r"ate\.dev_[a-z0-9_.-]+\.yaml\Z")


def read_url(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "agent-director-crd-updater"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError(f"Response exceeds size limit: {url}")
    return payload


def read_chart_archive(path: Path, name: str, version: str) -> dict[str, bytes]:
    """Read, never extract, a chart archive; reject links/traversal and oversized data."""
    files: dict[str, bytes] = {}
    # Bound the whole expanded stream, including directory/PAX headers, before
    # tarfile can parse metadata or retain members. Never decompress unboundedly.
    with gzip.open(path, "rb") as compressed:
        payload = compressed.read(MAX_ARTIFACT_BYTES + 1)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError("Chart archive exceeds expanded-stream size limit")
    total_size = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for count, member in enumerate(archive, start=1):
            if count > MAX_ARCHIVE_MEMBERS:
                raise ValueError("Chart archive exceeds member-count limit")
            member_path = PurePosixPath(member.name)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or "\\" in member.name
            ):
                raise ValueError(f"Unsafe chart archive path: {member.name}")
            if not member_path.parts or member_path.parts[0] != name:
                raise ValueError(f"Unexpected chart archive root: {member.name}")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(
                    f"Links and special files are forbidden in chart archives: {member.name}"
                )
            total_size += member.size
            if total_size > MAX_ARTIFACT_BYTES:
                raise ValueError("Chart archive exceeds size limit")
            key = str(member_path)
            if key in files:
                raise ValueError(f"Duplicate chart archive member: {key}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Unreadable chart archive member: {key}")
            with stream:
                files[key] = stream.read()
    metadata = yaml.safe_load(files.get(f"{name}/Chart.yaml", b""))
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != name
        or str(metadata.get("version")) != version
    ):
        raise ValueError(
            f"Chart archive metadata does not match locked {name} {version}"
        )
    return files


def to_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate schema nodes, not enum/default data or property-name mappings."""
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
    crd_directory: Path,
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
    outputs[crd_directory / filename] = content.decode("utf-8").rstrip() + "\n"
    return outputs


def postgres_outputs(root: Path) -> dict[Path, str]:
    chart = root / "charts/postgres-operator"
    dependencies = validate_lock(chart)
    operator = [
        dependency
        for dependency in dependencies
        if dependency["name"] == "postgres-operator"
    ]
    if len(operator) != 1:
        raise ValueError(
            "The postgres wrapper must lock exactly one postgres-operator dependency"
        )
    version = str(operator[0]["version"])
    artifact = chart / "charts" / f"postgres-operator-{version}.tgz"
    if not artifact.is_file():
        raise ValueError(
            f"Missing locked operator artifact {artifact}; run helm_dependencies.py build first"
        )
    files = read_chart_archive(artifact, "postgres-operator", version)
    outputs: dict[Path, str] = {}
    for name, content in sorted(files.items()):
        member = PurePosixPath(name)
        if member.parent != PurePosixPath(
            "postgres-operator/crds"
        ) or member.suffix not in {".yaml", ".yml"}:
            continue
        generated = crd_outputs(
            content,
            "acid.zalan.do",
            chart / "crds",
            root / ".schemas/acid.zalan.do",
            member.name,
        )
        if outputs.keys() & generated.keys():
            raise ValueError(f"Duplicate PostgreSQL CRD output in {name}")
        outputs.update(generated)
    if not outputs:
        raise ValueError(f"No PostgreSQL CRDs found in locked chart {artifact}")
    return outputs


def substrate_outputs(root: Path) -> dict[Path, str]:
    dockerfile = root / "images/agent-substrate/Dockerfile"
    match = SUBSTRATE_VERSION.search(dockerfile.read_text())
    if match is None:
        raise ValueError(f"Missing exact AGENT_SUBSTRATE_VERSION in {dockerfile}")
    version = match.group(1)
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
    outputs: dict[Path, str] = {}
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
            root / ".cache/olm-schema-inputs",
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
    # Fully validate/download the selected inputs before writing any tracked output.
    outputs: dict[Path, str] = {}
    if operation in {"postgres", "all"}:
        outputs.update(postgres_outputs(root))
    if operation in {"substrate", "all"}:
        outputs.update(substrate_outputs(root))
    if operation in {"olm", "all"}:
        outputs.update(olm_outputs(root))
    previous: set[Path] = set()
    providers = []
    if operation in {"postgres", "all"}:
        providers.append(("postgres-operator", "acid.zalan.do"))
    if operation in {"substrate", "all"}:
        providers.append(("agent-substrate", "ate.dev"))
    for chart_name, group in providers:
        crd_directory = root / "charts" / chart_name / "crds"
        for path in sorted(crd_directory.glob("*")):
            if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
                continue
            documents = list(yaml.safe_load_all(path.read_text()))
            if documents and all(
                isinstance(document, dict)
                and document.get("kind") == "CustomResourceDefinition"
                and document.get("spec", {}).get("group") == group
                for document in documents
            ):
                previous.add(path)
        # Each group-specific schema directory is owned by this generator.
        previous.update((root / ".schemas" / group).glob("*.json"))
    for path, content in sorted(outputs.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for obsolete in sorted(previous - outputs.keys()):
        obsolete.unlink()
    # Include deleted paths in the caller's owned output set as well.
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
        tarfile.TarError,
        yaml.YAMLError,
    ) as error:
        parser.exit(1, f"CRD update: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
