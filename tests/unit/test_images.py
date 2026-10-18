from pathlib import Path

import pytest

from scripts.images import exported_targets, image_definitions


def write_dockerfile(root: Path, text: str, name: str = "service") -> Path:
    path = root / "images" / name / "Dockerfile"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_multiline_build_keeps_unnamed_runtime(tmp_path: Path) -> None:
    path = write_dockerfile(
        tmp_path,
        "FROM python:3.14 AS builder\n"
        "RUN first-command && \\\n"
        "    second-command\n"
        "FROM scratch\nCOPY --from=builder /app /app\n",
    )
    assert exported_targets(path) == [""]
    assert image_definitions(tmp_path) == [
        {"name": "service", "path": "images/service/Dockerfile", "target": ""}
    ]


def test_named_frontend_runtime_keeps_component_name(tmp_path: Path) -> None:
    path = write_dockerfile(
        tmp_path,
        "FROM pnpm AS package-manager\n"
        "FROM node AS builder\n"
        "COPY --from=package-manager /pnpm /bin/pnpm\n"
        "FROM distroless AS runtime\n"
        "COPY --from=builder /app /app\n",
        "frontend",
    )
    assert exported_targets(path) == ["runtime"]
    assert image_definitions(tmp_path) == [
        {"name": "frontend", "path": "images/frontend/Dockerfile", "target": "runtime"}
    ]


def test_multiple_exports_keep_both_runtime_targets(tmp_path: Path) -> None:
    path = write_dockerfile(
        tmp_path,
        "FROM golang AS builder\n"
        "RUN compile-api && \\\n    compile-controller\n"
        "FROM scratch AS ateapi\nCOPY --from=builder /api /api\n"
        "FROM scratch AS atecontroller\nCOPY --from=builder /controller /controller\n",
        "agent-substrate",
    )
    assert exported_targets(path) == ["ateapi", "atecontroller"]
    assert [image["name"] for image in image_definitions(tmp_path)] == [
        "ateapi",
        "atecontroller",
    ]


def test_from_alias_numeric_copy_and_run_mount_are_dependencies(tmp_path: Path) -> None:
    path = write_dockerfile(
        tmp_path,
        "FROM base AS initial\n"
        "FROM initial AS intermediate\n"
        "FROM other AS runtime\n"
        "COPY --from=0 /base /base\n"
        "RUN --mount=type=bind,from=intermediate,target=/source compile\n",
    )
    assert exported_targets(path) == ["runtime"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("RUN nothing\n", "no build stages"),
        ("FROM\n", "invalid FROM"),
        ("FROM base AS same\nFROM other AS same\n", "duplicate stage alias"),
        ("FROM base\nRUN command " + chr(92), "unterminated line continuation"),
        ("FROM base\nRUN <<EOF\nFROM disguised\nEOF\n", "heredoc"),
    ],
)
def test_ambiguous_or_invalid_input_fails(
    tmp_path: Path, text: str, message: str
) -> None:
    path = write_dockerfile(tmp_path, text)
    with pytest.raises(ValueError, match=message):
        exported_targets(path)


def test_duplicate_export_names_fail(tmp_path: Path) -> None:
    write_dockerfile(tmp_path, "FROM base AS api\nFROM other AS worker\n", "multi")
    write_dockerfile(tmp_path, "FROM base\n", "api")
    with pytest.raises(ValueError, match="duplicate exported image"):
        image_definitions(tmp_path)


def test_missing_inventory_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no Dockerfiles"):
        image_definitions(tmp_path)


def test_repository_inventory_exports_substrate_and_frontend_correctly() -> None:
    images = {image["name"]: image for image in image_definitions()}
    assert images["ateapi"]["target"] == "ateapi"
    assert images["atecontroller"]["target"] == "atecontroller"
    assert images["frontend"]["path"] == "images/frontend/Dockerfile"
    assert images["frontend"]["target"] in {"runner", "runtime"}
    assert "agent-substrate" not in images
