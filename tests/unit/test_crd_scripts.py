"""Fixture-only provider maintenance, lock drift, and safe archive contracts."""

import importlib.util
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
import yaml

from scripts import helm_dependencies

ROOT = Path(__file__).absolute().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "update_crds", ROOT / "scripts/update-crds.py"
)
assert SPEC is not None and SPEC.loader is not None
CRDS = cast(Any, importlib.util.module_from_spec(SPEC))
SPEC.loader.exec_module(CRDS)


def _forbid_command(*args: object, **kwargs: object) -> NoReturn:
    pytest.fail(
        "No Helm command may run before lock validation or during offline check"
    )


def _forbid_download(url: str) -> NoReturn:
    pytest.fail(f"Postgres operation must not download substrate or master: {url}")


def _empty_listing(url: str) -> bytes:
    return b"[]"


def _wrapper(root: Path) -> Path:
    chart = root / "charts/postgres-operator"
    chart.mkdir(parents=True)
    declared = [
        {
            "name": "postgres-operator",
            "version": "2.0.3",
            "repository": "https://example.invalid/charts",
        }
    ]
    (chart / "Chart.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v2",
                "name": "postgres-wrapper",
                "version": "0.1.0",
                "dependencies": declared,
            }
        )
    )
    (chart / "Chart.lock").write_text(
        yaml.safe_dump(
            {
                "dependencies": declared,
                "digest": helm_dependencies.lock_digest(declared, declared),
            }
        )
    )
    return chart


def _crd(group: str = "acid.zalan.do", kind: str = "postgresql") -> bytes:
    return yaml.safe_dump(
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": f"{kind.lower()}s.{group}"},
            "spec": {
                "group": group,
                "names": {"kind": kind, "plural": kind.lower() + "s"},
                "versions": [
                    {
                        "name": "v1",
                        "served": True,
                        "storage": True,
                        "schema": {
                            "openAPIV3Schema": {
                                "type": "object",
                                "required": ["metadata", "spec"],
                                "properties": {"spec": {"type": "object"}},
                            }
                        },
                    }
                ],
            },
        },
    ).encode()


def _archive(
    chart: Path, version: str = "2.0.3", extra: tarfile.TarInfo | None = None
) -> Path:
    directory = chart / "charts"
    directory.mkdir(exist_ok=True)
    path = directory / "postgres-operator-2.0.3.tgz"
    files = {
        "postgres-operator/Chart.yaml": yaml.safe_dump(
            {"name": "postgres-operator", "version": version}
        ).encode(),
        "postgres-operator/crds/postgresqls.yaml": _crd(),
    }
    with tarfile.open(path, "w:gz") as archive:
        for filename, content in files.items():
            member = tarfile.TarInfo(filename)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        if extra is not None:
            archive.addfile(extra, io.BytesIO(b""))
    return path


def test_helm_dependency_build_is_lock_preserving_and_metadata_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chart = _wrapper(tmp_path)
    _archive(chart)
    original = (chart / "Chart.lock").read_bytes()
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["check"] is True
        calls.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, stdout="name: postgres-operator\nversion: 2.0.3\n"
        )

    monkeypatch.setattr(helm_dependencies.subprocess, "run", run)
    helm_dependencies.materialize([chart])
    assert calls[0][:3] == ["helm", "repo", "add"]
    assert calls[0][3].startswith("project-")
    assert calls[0][4:] == ["https://example.invalid/charts", "--force-update"]
    assert calls[1] == ["helm", "dependency", "build", str(chart), "--skip-refresh"]
    assert calls[2] == [
        "helm",
        "show",
        "chart",
        str(chart / "charts/postgres-operator-2.0.3.tgz"),
    ]
    assert all("update" not in call for call in calls)
    assert (chart / "Chart.lock").read_bytes() == original


def test_helm_dependency_check_is_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chart = _wrapper(tmp_path)
    monkeypatch.setattr(helm_dependencies.subprocess, "run", _forbid_command)
    helm_dependencies.materialize([chart], "check")


def test_helm_dependency_missing_lock_fails_before_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chart = _wrapper(tmp_path)
    (chart / "Chart.lock").unlink()
    monkeypatch.setattr(helm_dependencies.subprocess, "run", _forbid_command)
    with pytest.raises(ValueError, match="Missing committed dependency lock"):
        helm_dependencies.materialize([chart])


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "2.0.4"),
        ("repository", "https://different.invalid/charts"),
        ("condition", "operator.enabled"),
    ],
)
def test_helm_dependency_detects_declaration_and_digest_drift(
    tmp_path: Path, field: str, value: str
) -> None:
    chart = _wrapper(tmp_path)
    metadata = helm_dependencies.load_mapping(chart / "Chart.yaml")
    metadata["dependencies"][0][field] = value
    (chart / "Chart.yaml").write_text(yaml.safe_dump(metadata))
    with pytest.raises(ValueError, match="drift"):
        helm_dependencies.validate_lock(chart)


@pytest.mark.parametrize("remove_field", [False, True])
def test_removing_final_dependency_rejects_obsolete_lock(
    tmp_path: Path, remove_field: bool
) -> None:
    chart = _wrapper(tmp_path)
    metadata = helm_dependencies.load_mapping(chart / "Chart.yaml")
    if remove_field:
        metadata.pop("dependencies")
    else:
        empty_dependencies: list[dict[str, object]] = []
        metadata["dependencies"] = empty_dependencies
    (chart / "Chart.yaml").write_text(yaml.safe_dump(metadata))
    with pytest.raises(ValueError, match="Dependency count drift"):
        helm_dependencies.validate_lock(chart)


def test_empty_dependency_declaration_rejects_undeclared_cached_subchart(
    tmp_path: Path,
) -> None:
    chart = _wrapper(tmp_path)
    _archive(chart)
    metadata = helm_dependencies.load_mapping(chart / "Chart.yaml")
    metadata.pop("dependencies")
    (chart / "Chart.yaml").write_text(yaml.safe_dump(metadata))
    (chart / "Chart.lock").unlink()
    with pytest.raises(ValueError, match="Undeclared materialized subcharts"):
        helm_dependencies.validate_lock(chart)


@pytest.mark.parametrize("empty_declarations", [False, True])
def test_unpacked_undeclared_or_remote_override_subcharts_are_rejected(
    tmp_path: Path, empty_declarations: bool
) -> None:
    chart = _wrapper(tmp_path)
    unpacked = chart / "charts/stale"
    unpacked.mkdir(parents=True)
    (unpacked / "Chart.yaml").write_text("name: postgres-operator\nversion: 2.0.3\n")
    if empty_declarations:
        metadata = helm_dependencies.load_mapping(chart / "Chart.yaml")
        metadata.pop("dependencies")
        (chart / "Chart.yaml").write_text(yaml.safe_dump(metadata))
        (chart / "Chart.lock").unlink()
    with pytest.raises(ValueError, match="[Uu]ndeclared.*subchart"):
        helm_dependencies.validate_lock(chart)


def test_materialized_inventory_rejects_extra_archive_before_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chart = _wrapper(tmp_path)
    _archive(chart)
    (chart / "charts/obsolete-1.0.0.tgz").write_bytes(b"not loaded")
    monkeypatch.setattr(helm_dependencies.subprocess, "run", _forbid_command)
    with pytest.raises(ValueError, match="Materialized dependency drift"):
        helm_dependencies.validate_materialized(chart)


def test_materialized_inventory_checks_actual_archive_metadata(tmp_path: Path) -> None:
    chart = _wrapper(tmp_path)
    _archive(chart, version="2.0.2")
    with pytest.raises(ValueError, match="artifact metadata does not match locked"):
        helm_dependencies.validate_materialized(chart)


def test_declared_unpacked_local_dependency_remains_supported(tmp_path: Path) -> None:
    chart = _wrapper(tmp_path)
    metadata = helm_dependencies.load_mapping(chart / "Chart.yaml")
    metadata["dependencies"][0]["repository"] = ""
    declared = metadata["dependencies"]
    (chart / "Chart.yaml").write_text(yaml.safe_dump(metadata))
    (chart / "Chart.lock").write_text(
        yaml.safe_dump(
            {
                "dependencies": declared,
                "digest": helm_dependencies.lock_digest(declared, declared),
            }
        )
    )
    unpacked = chart / "charts/postgres-operator"
    unpacked.mkdir(parents=True)
    (unpacked / "Chart.yaml").write_text("name: postgres-operator\nversion: 2.0.3\n")
    assert helm_dependencies.validate_lock(chart)
    helm_dependencies.validate_materialized(chart)


def test_version_specific_scanner_exception_guard(tmp_path: Path) -> None:
    chart = _wrapper(tmp_path)
    helm_dependencies.require_versions([chart], ["postgres-operator=2.0.3"])
    with pytest.raises(ValueError, match="review exceptions before upgrading"):
        helm_dependencies.require_versions([chart], ["postgres-operator=2.0.4"])
    with pytest.raises(ValueError, match="Version guard must"):
        helm_dependencies.require_versions([chart], ["postgres-operator"])


def test_helm_digest_matches_native_committed_lock() -> None:
    assert helm_dependencies.validate_lock(ROOT / "charts/platform")


def test_postgres_operation_uses_locked_artifact_and_scopes_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chart = _wrapper(tmp_path)
    _archive(chart)
    substrate = tmp_path / "charts/agent-substrate/crds/existing.yaml"
    substrate.parent.mkdir(parents=True)
    substrate.write_text("unchanged\n")
    monkeypatch.setattr(CRDS, "read_url", _forbid_download)
    outputs = CRDS.update(tmp_path, "postgres")
    assert set(outputs) == {
        chart / "crds/postgresqls.yaml",
        tmp_path / ".schemas/acid.zalan.do/postgresql_v1.json",
    }
    assert substrate.read_text() == "unchanged\n"
    schema = json.loads(
        (tmp_path / ".schemas/acid.zalan.do/postgresql_v1.json").read_text()
    )
    assert schema["properties"]["apiVersion"]["enum"] == ["acid.zalan.do/v1"]
    assert schema["properties"]["kind"]["enum"] == ["postgresql"]
    assert set(schema["required"]) == {"metadata", "kind", "apiVersion", "spec"}
    assert not (tmp_path / ".schemas/ate.dev").exists()


def test_configuration_crd_preserves_upstream_shape_without_requiring_spec(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load(_crd(kind="OperatorConfiguration"))
    schema = document["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    schema["required"] = ["configuration", "metadata"]
    schema["properties"] = {"configuration": {"type": "object"}}
    outputs = CRDS.crd_outputs(
        yaml.safe_dump(document).encode(),
        "acid.zalan.do",
        tmp_path / "crds",
        tmp_path / "schemas",
        "operatorconfigurations.yaml",
    )
    generated = json.loads(outputs[tmp_path / "schemas/operatorconfiguration_v1.json"])
    assert set(generated["required"]) == {
        "configuration",
        "metadata",
        "kind",
        "apiVersion",
    }
    assert "spec" not in generated["properties"]


def test_nullable_conversion_preserves_data_and_nullable_property_names() -> None:
    original = {
        "type": "object",
        "properties": {
            "nullable": {"type": "boolean"},
            "field": {"type": "array", "nullable": True, "items": {"type": "string"}},
        },
        "default": {"nullable": True},
        "enum": [{"nullable": True}],
    }
    converted = CRDS.to_json_schema(original)
    assert converted["properties"]["nullable"] == {"type": "boolean"}
    assert converted["default"] == {"nullable": True}
    assert converted["enum"] == [{"nullable": True}]
    assert converted["properties"]["field"] == {
        "anyOf": [{"type": "null"}, {"type": "array", "items": {"type": "string"}}]
    }
    assert original["properties"]["field"]["nullable"] is True


@pytest.mark.parametrize(
    "value,valid", [(None, True), (["127.0.0.1/32"], True), ("invalid-scalar", False)]
)
def test_generated_nullable_schema_matches_kubeconform_draft4(
    tmp_path: Path, value: object, valid: bool
) -> None:
    executable = shutil.which("kubeconform")
    assert executable is not None, "Run schema controls through the root mise toolchain"
    document = yaml.safe_load(_crd())
    document["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"][
        "spec"
    ] = {
        "type": "object",
        "properties": {
            "allowedSourceRanges": {
                "type": "array",
                "nullable": True,
                "items": {"type": "string"},
            }
        },
    }
    schemas = tmp_path / "schemas"
    outputs = CRDS.crd_outputs(
        yaml.safe_dump(document).encode(),
        "acid.zalan.do",
        tmp_path / "crds",
        schemas,
        "postgresqls.yaml",
    )
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    resource = tmp_path / "resource.yaml"
    resource.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "acid.zalan.do/v1",
                "kind": "postgresql",
                "metadata": {"name": "nullable-control"},
                "spec": {"allowedSourceRanges": value},
            }
        )
    )
    result = subprocess.run(
        [
            executable,
            "-strict",
            "-summary",
            "-schema-location",
            str(schemas / "postgresql_v1.json"),
            str(resource),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is valid, result.stdout + result.stderr
    assert "1 resource" in result.stdout + result.stderr


def test_selected_update_prunes_obsolete_provider_outputs_only(tmp_path: Path) -> None:
    chart = _wrapper(tmp_path)
    _archive(chart)
    CRDS.update(tmp_path, "postgres")
    obsolete_crd = chart / "crds/removed.yaml"
    obsolete_crd.write_bytes(_crd(kind="RemovedResource"))
    obsolete_schema = tmp_path / ".schemas/acid.zalan.do/postgresql_v1beta1.json"
    obsolete_schema.write_text("{}\n")
    unrelated = chart / "crds/unrelated.yaml"
    unrelated.write_text("kind: ConfigMap\n")
    substrate = tmp_path / ".schemas/ate.dev/existing_v1.json"
    substrate.parent.mkdir(parents=True)
    substrate.write_text("{}\n")
    outputs = CRDS.update(tmp_path, "postgres")
    assert obsolete_crd in outputs and obsolete_schema in outputs
    assert not obsolete_crd.exists() and not obsolete_schema.exists()
    assert unrelated.read_text() == "kind: ConfigMap\n"
    assert substrate.read_text() == "{}\n"


def test_postgres_rejects_artifact_version_mismatch(tmp_path: Path) -> None:
    chart = _wrapper(tmp_path)
    _archive(chart, version="2.0.2")
    with pytest.raises(ValueError, match="metadata does not match locked"):
        CRDS.update(tmp_path, "postgres")
    assert not (chart / "crds").exists()


def test_postgres_requires_materialized_locked_artifact(tmp_path: Path) -> None:
    _wrapper(tmp_path)
    with pytest.raises(ValueError, match="Missing locked operator artifact"):
        CRDS.update(tmp_path, "postgres")


@pytest.mark.parametrize(
    "filename",
    [
        "../escape",
        "/absolute",
        "postgres-operator/../escape",
        "postgres-operator/\\escape",
    ],
)
def test_postgres_archive_rejects_unsafe_paths(tmp_path: Path, filename: str) -> None:
    chart = _wrapper(tmp_path)
    _archive(chart, extra=tarfile.TarInfo(filename))
    with pytest.raises(ValueError, match="Unsafe chart archive path"):
        CRDS.update(tmp_path, "postgres")
    assert not (chart / "crds").exists()


def test_postgres_bounds_expanded_stream_before_tar_metadata_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chart = _wrapper(tmp_path)
    _archive(chart)
    monkeypatch.setattr(CRDS, "MAX_ARTIFACT_BYTES", 1024)
    monkeypatch.setattr(CRDS.tarfile, "open", _forbid_command)
    with pytest.raises(ValueError, match="expanded-stream size limit"):
        CRDS.update(tmp_path, "postgres")
    assert not (chart / "crds").exists()


def test_postgres_bounds_directory_member_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chart = _wrapper(tmp_path)
    directory = tarfile.TarInfo("postgres-operator/extra")
    directory.type = tarfile.DIRTYPE
    _archive(chart, extra=directory)
    monkeypatch.setattr(CRDS, "MAX_ARCHIVE_MEMBERS", 2)
    with pytest.raises(ValueError, match="member-count limit"):
        CRDS.update(tmp_path, "postgres")
    assert not (chart / "crds").exists()


def test_postgres_archive_rejects_symlinks(tmp_path: Path) -> None:
    chart = _wrapper(tmp_path)
    link = tarfile.TarInfo("postgres-operator/crds/link.yaml")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../outside"
    _archive(chart, extra=link)
    with pytest.raises(ValueError, match="Links and special files"):
        CRDS.update(tmp_path, "postgres")


def test_substrate_operation_retains_dockerfile_version_and_scopes_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dockerfile = tmp_path / "images/agent-substrate/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("ARG AGENT_SUBSTRATE_VERSION=v0.3.0\n")
    urls: list[str] = []

    def read_url(url: str) -> bytes:
        urls.append(url)
        if "api.github.com" in url:
            return json.dumps(
                [{"name": "ate.dev_sandboxes.yaml"}, {"name": "../../invalid.yaml"}]
            ).encode()
        return _crd("ate.dev", "Sandbox")

    monkeypatch.setattr(CRDS, "read_url", read_url)
    outputs = CRDS.update(tmp_path, "substrate")
    assert set(outputs) == {
        tmp_path / "charts/agent-substrate/crds/ate.dev_sandboxes.yaml",
        tmp_path / ".schemas/ate.dev/sandbox_v1.json",
    }
    assert "?ref=v0.3.0" in urls[0]
    assert "/v0.3.0/" in urls[1]
    assert not (tmp_path / "charts/postgres-operator").exists()
    assert not (tmp_path / ".schemas/acid.zalan.do").exists()


def test_updater_callers_materialize_and_stage_selected_output_sets() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert "update-schemas: check-helm-deps" in makefile
    assert "scripts/update-crds.py all" in makefile
    renovate = json.loads((ROOT / "renovate.json").read_text())
    substrate = next(
        rule["postUpgradeTasks"]
        for rule in renovate["packageRules"]
        if "agent-substrate/substrate" in rule.get("matchPackageNames", [])
    )
    assert any(
        "scripts/update-crds.py substrate" in command
        for command in substrate["commands"]
    )
    assert any(
        path.startswith("charts/agent-substrate/crds/")
        for path in substrate["fileFilters"]
    )
    assert any(
        path.startswith(".schemas/ate.dev/") for path in substrate["fileFilters"]
    )
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/update-schemas.yml").read_text()
    )
    runs = "\n".join(
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job["steps"]
    )
    staging = [line for line in runs.splitlines() if "git add" in line]
    assert any(
        ".schemas" in line
        and "charts/postgres-operator/crds" in line
        and "charts/agent-substrate/crds" in line
        for line in staging
    )


def test_all_operation_validates_all_inputs_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chart = _wrapper(tmp_path)
    _archive(chart)
    dockerfile = tmp_path / "images/agent-substrate/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("ARG AGENT_SUBSTRATE_VERSION=v0.3.0\n")
    monkeypatch.setattr(CRDS, "read_url", _empty_listing)
    with pytest.raises(ValueError, match="No substrate CRDs"):
        CRDS.update(tmp_path, "all")
    assert not (chart / "crds").exists()
    assert not (tmp_path / ".schemas").exists()
