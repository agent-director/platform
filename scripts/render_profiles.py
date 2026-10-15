#!/usr/bin/env -S uv run
"""Render first-party chart profiles for offline gates."""

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).absolute().parent.parent
PROFILE_FILES = (
    "default.yaml",
    "kind.yaml",
    "substrate.yaml",
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


def render_profiles(
    root: Path, output: Path, release: str = "platform", namespace: str = "platform"
) -> None:
    platform = root / "charts/platform"
    substrate = root / "charts/agent-substrate"
    common = [
        "--set-string",
        "secrets.langfuseNextauthSecret=local-validation-only",
        "--set-string",
        "secrets.langfuseSalt=local-validation-only",
    ]
    rendered = {
        "default.yaml": helm_template(release, platform, namespace, common),
        "kind.yaml": helm_template(
            release,
            platform,
            namespace,
            [*common, "-f", str(platform / "values-kind.yaml")],
        ),
        "substrate.yaml": helm_template(
            "agent-substrate", substrate, "agent-substrate", []
        ),
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
