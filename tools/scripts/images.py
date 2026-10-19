#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Discover exported Docker targets for CI, integration and local scans."""

import argparse
import json
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")


def exported_targets(dockerfile: Path) -> list[str]:
    stages: list[str | None] = []
    aliases: dict[str, int] = {}
    consumed: set[int] = set()

    def consume(reference: str) -> None:
        key = reference.lower()
        if key in aliases:
            consumed.add(aliases[key])
        elif reference.isdecimal() and int(reference) < len(stages):
            consumed.add(int(reference))

    logical_lines: list[str] = []
    continued = ""
    for line in dockerfile.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.rstrip().endswith("\\"):
            continued += line.rstrip()[:-1] + " "
            continue
        logical_lines.append(continued + line)
        continued = ""
    if continued:
        raise ValueError(f"unterminated line continuation in {dockerfile}")

    for line in logical_lines:
        instruction = line.split(maxsplit=1)[0].upper()
        if instruction not in {"FROM", "COPY", "ADD", "RUN"}:
            continue
        tokens = shlex.split(line, comments=True)
        if any(token.startswith("<<") for token in tokens):
            raise ValueError(
                f"heredoc image discovery is not supported in {dockerfile}"
            )
        if instruction == "FROM":
            arguments = tokens[1:]
            while arguments and arguments[0].startswith("--"):
                arguments.pop(0)
            if not arguments:
                raise ValueError(f"invalid FROM instruction in {dockerfile}")
            consume(arguments[0])
            target = None
            if len(arguments) > 1:
                if len(arguments) != 3 or arguments[1].upper() != "AS":
                    raise ValueError(f"invalid stage alias in {dockerfile}")
                target = arguments[2]
                if target.lower() in aliases:
                    raise ValueError(
                        f"duplicate stage alias {target!r} in {dockerfile}"
                    )
                aliases[target.lower()] = len(stages)
            stages.append(target)
        elif instruction in {"COPY", "ADD"}:
            for token in tokens[1:]:
                if token.startswith("--from="):
                    consume(token.partition("=")[2])
        elif instruction == "RUN":
            for token in tokens[1:]:
                if token.startswith("--mount="):
                    for option in token.partition("=")[2].split(","):
                        if option.startswith("from="):
                            consume(option.partition("=")[2])

    if not stages:
        raise ValueError(f"no build stages in {dockerfile}")
    targets = [
        target or ""
        for index, target in enumerate(stages)
        if index not in consumed and (target is not None or index == len(stages) - 1)
    ]
    if not targets:
        raise ValueError(f"no exported build targets in {dockerfile}")
    return targets


def image_definitions(root: Path = ROOT) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    names: set[str] = set()
    for dockerfile in sorted((root / "images").glob("*/Dockerfile")):
        targets = exported_targets(dockerfile)
        for target in targets:
            name = target if len(targets) > 1 else dockerfile.parent.name
            if IMAGE_NAME.fullmatch(name) is None:
                raise ValueError(
                    f"invalid exported image name {name!r} in {dockerfile}"
                )
            if name in names:
                raise ValueError(f"duplicate exported image name {name!r}")
            names.add(name)
            images.append(
                {
                    "name": name,
                    "path": str(dockerfile.relative_to(root)),
                    "target": target,
                }
            )
    if not images:
        raise ValueError("no Dockerfiles found under images/")
    return images


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["json", "matrix", "tsv"], default="json")
    arguments = parser.parse_args()
    try:
        images = image_definitions()
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if arguments.format == "tsv":
        for image in images:
            print("\t".join(image[key] for key in ("name", "path", "target")))
    elif arguments.format == "matrix":
        print(json.dumps({"component": images}, separators=(",", ":")))
    else:
        print(json.dumps(images, separators=(",", ":")))


if __name__ == "__main__":
    main()
