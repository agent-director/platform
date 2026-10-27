"""Fixture-only provider schema and CRD maintenance tests."""

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).absolute().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "update_crds", ROOT / "tools/scripts/update-crds.py"
)
assert SPEC is not None and SPEC.loader is not None
CRDS = cast(Any, importlib.util.module_from_spec(SPEC))
SPEC.loader.exec_module(CRDS)


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


def _setup_deploy_script(root: Path, version: str = "2.0.3") -> Path:
    script = root / "tools/scripts/deploy.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"POSTGRES_OPERATOR_VERSION={version}\n")
    return script


def test_to_json_schema_converts_nullable_nodes_only() -> None:
    raw = {
        "type": "object",
        "required": ["metadata", "spec"],
        "properties": {
            "nullable": {"type": "string", "nullable": True},
            "enumField": {"type": "string", "enum": ["nullable"]},
            "itemsField": {
                "type": "array",
                "items": {"type": "string", "nullable": True},
            },
        },
    }
    converted = CRDS.to_json_schema(raw)
    assert converted["required"] == ["metadata", "spec"]
    assert converted["properties"]["nullable"] == {
        "anyOf": [{"type": "null"}, {"type": "string"}]
    }
    assert converted["properties"]["enumField"] == {
        "type": "string",
        "enum": ["nullable"],
    }
    assert converted["properties"]["itemsField"]["items"] == {
        "anyOf": [{"type": "null"}, {"type": "string"}]
    }


@pytest.mark.parametrize("valid", [True, False])
def test_generated_postgres_schema_enforces_kubeconform_strict_contract(
    tmp_path: Path, valid: bool
) -> None:
    executable = shutil.which("kubeconform")
    assert executable is not None
    _setup_deploy_script(tmp_path)
    schemas = ROOT / ".schemas"
    resource = tmp_path / "db.yaml"
    manifest: dict[str, Any] = {
        "apiVersion": "acid.zalan.do/v1",
        "kind": "postgresql",
        "metadata": {"name": "platform-db"},
        "spec": {
            "teamId": "agent-director",
            "numberOfInstances": 1,
            "volume": {"size": "10Gi"},
            "postgresql": {"version": "18"},
            "additionalVolumes": [
                {
                    "name": "tls",
                    "mountPath": "/tls",
                    "targetContainers": None,
                    "volumeSource": {"secret": {"secretName": "db-tls"}},
                }
            ],
        },
    }
    if not valid:
        manifest["spec"]["postgresql"]["version"] = "99"
    resource.write_text(yaml.safe_dump(manifest))
    result = subprocess.run(
        [
            executable,
            "-strict",
            "-summary",
            "-schema-location",
            f"{schemas}/{{{{.Group}}}}/{{{{.ResourceKind}}}}_{{{{.ResourceAPIVersion}}}}.json",
            str(resource),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is valid, result.stdout + result.stderr
    assert "1 resource" in result.stdout + result.stderr


def test_selected_update_prunes_obsolete_provider_outputs_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_deploy_script(tmp_path, "2.0.3")

    def read_url(_url: str) -> bytes:
        return _crd()

    monkeypatch.setattr(CRDS, "read_url", read_url)
    CRDS.update(tmp_path, "postgres")
    obsolete_schema = tmp_path / ".schemas/acid.zalan.do/postgresteam_v1.json"
    obsolete_schema.write_text("{}\n")
    substrate = tmp_path / ".schemas/ate.dev/existing_v1.json"
    substrate.parent.mkdir(parents=True)
    substrate.write_text("{}\n")
    outputs = CRDS.update(tmp_path, "postgres")
    assert obsolete_schema in outputs
    assert not obsolete_schema.exists()
    assert (tmp_path / ".schemas/acid.zalan.do/postgresql_v1.json").is_file()
    assert substrate.read_text() == "{}\n"


def test_substrate_operation_retains_dockerfile_version_and_scopes_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dockerfile = tmp_path / "images/agent-substrate/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("ARG AGENT_SUBSTRATE_VERSION=v0.4.0\n")
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
    assert "?ref=v0.4.0" in urls[0]
    assert "/v0.4.0/" in urls[1]
    assert not (tmp_path / ".schemas/acid.zalan.do").exists()


def test_updater_callers_stage_selected_output_sets() -> None:
    assert CRDS.ROOT == ROOT
    makefile = (ROOT / "Makefile").read_text()
    assert "tools/scripts/update-crds.py all" in makefile
    renovate = json.loads((ROOT / "renovate.json").read_text())
    rule = next(
        r["postUpgradeTasks"]
        for r in renovate["packageRules"]
        if "agent-substrate/substrate" in r.get("matchPackageNames", [])
    )
    assert any(
        "tools/scripts/update-crds.py all" in command for command in rule["commands"]
    )
    assert "charts/agent-substrate/crds/*.yaml" in rule["fileFilters"]
    assert ".schemas/**/*.json" in rule["fileFilters"]
    manager_patterns = renovate["customManagers"][0]["managerFilePatterns"]
    assert "/^tools/scripts/.*\\.(?:sh|py)$/" in manager_patterns
    assert not any(pattern.startswith("/^scripts/") for pattern in manager_patterns)
    match_regex = re.compile(
        renovate["customManagers"][0]["matchStrings"][0].replace("(?<", "(?P<")
    )
    for rel_path, expected_dep in (
        ("Makefile", "checkov"),
        ("tools/scripts/deploy.sh", "zalando/postgres-operator"),
        ("images/agent-substrate/Dockerfile", "agent-substrate/substrate"),
        ("images/platform-worker/Dockerfile", "python"),
        ("images/sandbox/Dockerfile", "python"),
    ):
        matches = [
            m.group("depName")
            for m in match_regex.finditer((ROOT / rel_path).read_text())
        ]
        assert expected_dep in matches, rel_path
    python_rule = next(
        r for r in renovate["packageRules"] if r.get("groupName") == "python"
    )
    assert python_rule.get("allowedVersions") == "<3.15"
    assert {"python", "python/cpython", "docker.io/library/python"} <= set(
        python_rule.get("matchPackageNames", [])
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
        ".schemas/" in line and "charts/agent-substrate/crds/" in line
        for line in staging
    )


def test_all_operation_validates_all_inputs_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_deploy_script(tmp_path, "2.0.3")
    dockerfile = tmp_path / "images/agent-substrate/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("ARG AGENT_SUBSTRATE_VERSION=v0.4.0\n")

    def read_url(url: str) -> bytes:
        if "zalando/postgres-operator" in url:
            return _crd()
        return b"[]"

    monkeypatch.setattr(CRDS, "read_url", read_url)
    with pytest.raises(ValueError, match="No substrate CRDs"):
        CRDS.update(tmp_path, "all")
    assert not (tmp_path / ".schemas").exists()
