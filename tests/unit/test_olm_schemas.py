import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.unit.test_crd_scripts import CRDS

REVISION = "5359e60525288af86885e14d89e3210878f87274"


def source_chart(root: Path, revision: str = REVISION) -> None:
    chart = root / "charts/platform/Chart.yaml"
    chart.parent.mkdir(parents=True)
    chart.write_text(
        yaml.safe_dump({"annotations": {"agent-director.io/olm-schema-ref": revision}})
    )


def source_crd(kind: str, version: str, served: bool = True) -> bytes:
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["spec"],
        "properties": {"spec": {"type": "object", "nullable": True}},
    }
    return yaml.safe_dump(
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": f"{kind.lower()}s.operators.coreos.com"},
            "spec": {
                "group": "operators.coreos.com",
                "names": {"kind": kind},
                "versions": [
                    {
                        "name": version,
                        "served": served,
                        "schema": {"openAPIV3Schema": schema},
                    }
                ],
            },
        }
    ).encode()


def test_olm_schema_update_uses_pinned_served_versions_without_installing_crds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_chart(tmp_path)
    requested: list[str] = []

    def read_url(url: str) -> bytes:
        requested.append(url)
        assert f"/{REVISION}/manifests/" in url
        if "operatorgroups" in url:
            return source_crd("OperatorGroup", "v1")
        return source_crd("Subscription", "v1alpha1")

    monkeypatch.setattr(CRDS, "read_url", read_url)
    paths = CRDS.update(tmp_path, "olm")
    assert len(requested) == 2
    assert {path.name for path in paths} == {
        "operatorgroup_v1.json",
        "subscription_v1alpha1.json",
    }
    for path in paths:
        schema = json.loads(path.read_text())
        assert REVISION in schema["$comment"]
        assert schema["properties"]["apiVersion"]["enum"][0].startswith(
            "operators.coreos.com/"
        )
        assert "anyOf" in schema["properties"]["spec"]
    assert not list((tmp_path / "charts").rglob("crds/*.yaml"))


@pytest.mark.parametrize("revision", ["latest", "release-4.22", "a" * 39])
def test_olm_schema_source_requires_immutable_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    source_chart(tmp_path, revision)

    def no_download(_url: str) -> bytes:
        pytest.fail("Invalid revision must fail before fetching upstream data")

    monkeypatch.setattr(CRDS, "read_url", no_download)
    with pytest.raises(ValueError, match="immutable"):
        CRDS.update(tmp_path, "olm")


def test_olm_unserved_version_fails_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_chart(tmp_path)

    def read_url(url: str) -> bytes:
        if "operatorgroups" in url:
            return source_crd("OperatorGroup", "v1")
        return source_crd("Subscription", "v1alpha1", served=False)

    monkeypatch.setattr(CRDS, "read_url", read_url)
    with pytest.raises(ValueError, match="served CRD schemas"):
        CRDS.update(tmp_path, "olm")
    assert not (tmp_path / ".schemas").exists()
