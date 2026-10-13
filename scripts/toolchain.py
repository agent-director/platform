#!/usr/bin/env -S uv run --no-project
"""Project metadata projections and checks; installation belongs to standard mise.

Standard-library version/image/build-args/project queries need no application sync:
  scripts/mise.sh exec -- uv run --no-project scripts/toolchain.py COMMAND
Dependency-backed checks and hook setup use the locked project environment:
  scripts/mise.sh exec -- uv run --locked scripts/toolchain.py check
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import cast

ROOT = Path(os.path.abspath(__file__)).parent.parent
EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+\Z")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
SKIP_DIRECTORIES = {
    ".cache",
    ".venv",
    "venv",
    ".git",
    "hack",
    "node_modules",
    "__pycache__",
}
BUILD_TOOLS = ("python", "uv", "node", "pnpm")
BUILD_IMAGES = ("uv", "node", "pnpm", "node_runtime")
HOOK_BEGIN = "# BEGIN platform root toolchain environment\n"
HOOK_END = "# END platform root toolchain environment\n"
CACHE_PATHS = {
    "PLATFORM_CACHE_DIR": "",
    "UV_CACHE_DIR": "/uv",
    "PREK_HOME": "/prek",
    "RUFF_CACHE_DIR": "/ruff",
    "PYTHONPYCACHEPREFIX": "/pycache",
    "TMPDIR": "/tmp",
    "TMP": "/tmp",
    "TEMP": "/tmp",
    "HELM_CACHE_HOME": "/helm/cache",
    "HELM_CONFIG_HOME": "/helm/config",
    "HELM_DATA_HOME": "/helm/data",
    "TRIVY_CACHE_DIR": "/trivy",
    "COVERAGE_FILE": "/coverage/.coverage",
    "pnpm_config_store_dir": "/pnpm/store",
    "pnpm_config_cache_dir": "/pnpm/cache",
    "pnpm_config_state_dir": "/pnpm/state",
    "XDG_CACHE_HOME": "/xdg",
    "XDG_RUNTIME_DIR": "/xdg-runtime",
    "MISE_CACHE_DIR": "/mise/cache",
    "MISE_STATE_DIR": "/mise/state",
    "MISE_DOWNLOADS_DIR": "/mise/downloads",
    "MISE_TMP_DIR": "/mise/tmp",
    "MISE_CONFIG_DIR": "/mise/config",
    "MISE_SYSTEM_CONFIG_DIR": "/mise/system",
}


class ToolchainError(ValueError):
    """The checked-in contract is inconsistent or malformed."""


def table(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ToolchainError(f"{label} must be a table")
    return cast(dict[str, object], value)


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ToolchainError(f"{label} must be a nonempty string")
    return value


def load_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        return table(tomllib.load(stream), str(path))


def load_json(path: Path) -> dict[str, object]:
    return table(json.loads(path.read_text()), str(path))


def version(manifest: dict[str, object], tool: str) -> str:
    selected = text(table(manifest.get("tools"), "tools").get(tool), f"tools.{tool}")
    if not EXACT_VERSION.fullmatch(selected):
        raise ToolchainError(
            f"tools.{tool} must select an exact release, got {selected!r}"
        )
    return selected


def image(manifest: dict[str, object], name: str) -> str:
    metadata = table(manifest.get("_"), "_")
    images = table(metadata.get("images"), "_.images")
    spec = table(images.get(name), f"_.images.{name}")
    repository = text(spec.get("repository"), f"{name}.repository")
    repository = repository.format(node_major=version(manifest, "node").split(".")[0])
    digest = text(spec.get("digest"), f"{name}.digest")
    if not DIGEST.fullmatch(digest):
        raise ToolchainError(f"{name}.digest must be a SHA-256 image digest")
    if "version" in spec:
        tag = text(spec["version"], f"{name}.version")
    else:
        suffix = spec.get("suffix", "")
        if not isinstance(suffix, str):
            raise ToolchainError(f"{name}.suffix must be a string")
        tag = version(manifest, name) + suffix
    if not re.fullmatch(r"[a-zA-Z0-9._/-]+", repository) or not re.fullmatch(
        r"[a-zA-Z0-9._-]+", tag
    ):
        raise ToolchainError(f"unsafe image repository/tag for {name}")
    return f"{repository}:{tag}@{digest}"


def build_args(manifest: dict[str, object]) -> dict[str, str]:
    args = {f"{tool.upper()}_VERSION": version(manifest, tool) for tool in BUILD_TOOLS}
    args.update(
        {f"{name.upper()}_IMAGE": image(manifest, name) for name in BUILD_IMAGES}
    )
    return args


def project_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in SKIP_DIRECTORIES)
        files.extend(Path(directory) / name for name in sorted(filenames))
    return files


def is_override(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    name = path.name
    if relative.as_posix() in {"mise.toml", ".python-version", "pnpm-workspace.yaml"}:
        return False
    if name in {
        ".python-version",
        ".python-versions",
        ".node-version",
        ".nvmrc",
        ".tool-versions",
        "uv.toml",
        "pnpm-workspace.yaml",
    }:
        return True
    if re.fullmatch(r"\.?mise(?:\.[\w-]+)*\.toml", name) or name.startswith(".miserc"):
        return True
    parts = relative.parts
    if ".config" in parts[:-1] and name.startswith("miserc") and name.endswith(".toml"):
        return True
    return (
        name.endswith(".toml")
        and any(part in {"mise", ".mise"} for part in parts[:-1])
        and (name.startswith("config") or "conf.d" in parts)
    )


def check_native_metadata(root: Path, manifest: dict[str, object]) -> list[str]:
    import yaml
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import Version as PythonVersion
    from semantic_version import NpmSpec
    from semantic_version import Version as NodeVersion

    errors: list[str] = []
    python = version(manifest, "python")
    projection = root / ".python-version"
    if not projection.is_file() or projection.read_text() != f"{python}\n":
        errors.append(
            ".python-version differs from tools.python; run toolchain.py project"
        )
    pyproject = load_toml(root / "pyproject.toml")
    requires_python = text(
        table(pyproject.get("project"), "project").get("requires-python"),
        "requires-python",
    )
    try:
        if PythonVersion(python) not in SpecifierSet(requires_python):
            errors.append(
                f"Python {python} does not satisfy requires-python {requires_python}"
            )
    except InvalidSpecifier as error:
        errors.append(f"invalid requires-python: {error}")

    for path in project_files(root):
        if is_override(path, root):
            errors.append(
                f"unapproved child/alternate toolchain override: {path.relative_to(root)}"
            )
        if path.name != "package.json":
            continue
        package = load_json(path)
        relative = path.relative_to(root)
        if path != root / "package.json":
            if "packageManager" in package or "volta" in package:
                errors.append(
                    f"{relative} contains an independent package-manager/runtime selection"
                )
            dev_engines = table(package.get("devEngines", {}), f"{relative}.devEngines")
            if "runtime" in dev_engines or "packageManager" in dev_engines:
                errors.append(
                    f"{relative} contains an independent devEngines selection"
                )
        engines = table(package.get("engines", {}), f"{relative}.engines")
        for tool in ("node", "pnpm"):
            if tool not in engines:
                continue
            constraint = text(engines[tool], f"{relative}.engines.{tool}")
            try:
                if not NpmSpec(constraint).match(NodeVersion(version(manifest, tool))):
                    errors.append(
                        f"{relative}: {tool} {version(manifest, tool)} does not satisfy {constraint}"
                    )
            except ValueError as error:
                errors.append(
                    f"{relative}: invalid {tool} compatibility range: {error}"
                )

    package = load_json(root / "package.json")
    if package.get("packageManager") != f"pnpm@{version(manifest, 'pnpm')}":
        errors.append("package.json packageManager differs from tools.pnpm")
    runtime = table(
        table(package.get("devEngines"), "devEngines").get("runtime"),
        "devEngines.runtime",
    )
    if runtime != {
        "name": "node",
        "version": version(manifest, "node"),
        "onFail": "error",
    }:
        errors.append(
            "package.json devEngines.runtime differs from tools.node/error policy"
        )

    workspace = table(
        yaml.safe_load((root / "pnpm-workspace.yaml").read_text()), "pnpm workspace"
    )
    if workspace.get("packages") != ["frontend"]:
        errors.append("pnpm workspace must contain exactly the frontend package")
    for key, expected in {
        "storeDir": "${PLATFORM_CACHE_DIR}/pnpm/store",
        "cacheDir": "${PLATFORM_CACHE_DIR}/pnpm/cache",
        "pmOnFail": "error",
        "runtimeOnFail": "error",
        "engineStrict": True,
    }.items():
        if workspace.get(key) != expected:
            errors.append(f"pnpm workspace {key} must be {expected!r}")
    return errors


def check_manifest(manifest: dict[str, object]) -> list[str]:
    errors: list[str] = []
    for tool in table(manifest.get("tools"), "tools"):
        version(manifest, tool)
    settings = table(manifest.get("settings"), "settings")
    if settings.get("lockfile") is not True:
        errors.append("mise settings.lockfile must remain true")
    if table(manifest.get("tool_config", {}), "tool_config").get("locked") is not True:
        errors.append("mise tool_config.locked must remain true")
    if table(settings.get("python"), "settings.python").get("uv_venv_auto") != "source":
        errors.append('mise settings.python.uv_venv_auto must remain "source"')
    env = table(manifest.get("env"), "env")
    if env.get("UV_PYTHON") != {"value": "{{ tools.python.path }}", "tools": True}:
        errors.append("UV_PYTHON must explicitly select mise's installed interpreter")
    if env.get("UV_PROJECT_ENVIRONMENT") != "{{ config_root }}/.venv":
        errors.append("uv must retain ownership of the root .venv")
    if any(
        key in env
        for key in ("MISE_DATA_DIR", "MISE_INSTALLS_DIR", "UV_PYTHON_INSTALL_DIR")
    ):
        errors.append(
            "installed interpreter/tool storage must not be relocated into disposable caches"
        )
    for key, suffix in CACHE_PATHS.items():
        if env.get(key) != "{{ config_root }}/.cache" + suffix:
            errors.append(f"mise {key} must use the root-absolute project cache path")
    metadata = table(manifest.get("_"), "_")
    if metadata.get("mise_version") != manifest.get("min_version"):
        errors.append("mise bootstrap version and min_version differ")
    for name in table(metadata.get("images"), "_.images"):
        image(manifest, name)
    node_kubernetes = text(
        table(table(metadata.get("images"), "images").get("kind"), "kind").get(
            "version"
        ),
        "kind image version",
    )
    client_minor = int(version(manifest, "kubectl").split(".")[1])
    server_minor = int(node_kubernetes.removeprefix("v").split(".")[1])
    if abs(client_minor - server_minor) > 1:
        errors.append(
            "kubectl and kind node Kubernetes image exceed the supported one-minor skew"
        )
    return errors


def check_lock(root: Path, manifest: dict[str, object]) -> list[str]:
    lock = load_toml(root / "mise.lock")
    locked_tools = table(lock.get("tools"), "mise.lock tools")
    errors: list[str] = []
    for tool in table(manifest.get("tools"), "tools"):
        entries = locked_tools.get(tool)
        if not isinstance(entries, list) or len(entries) != 1:
            errors.append(f"mise.lock must have exactly one entry for {tool}")
            continue
        entry = table(entries[0], f"mise.lock {tool}")
        if entry.get("version") != version(manifest, tool):
            errors.append(f"mise.lock {tool} differs from root pin")
        if entry.get("specifiers") != [version(manifest, tool)]:
            errors.append(f"mise.lock {tool} request binding differs from root pin")
        # Native mise format 3 stores platform tables under quoted dotted keys,
        # not a nested `platforms` mapping. Do not rewrite its native format.
        for platform in ("linux-x64", "linux-arm64", "macos-x64", "macos-arm64"):
            artifact = table(
                entry.get(f"platforms.{platform}", {}), f"{tool} {platform}"
            )
            checksum = artifact.get("checksum")
            url = artifact.get("url")
            if (
                not isinstance(url, str)
                or not url.startswith("https://")
                or not isinstance(checksum, str)
                or not DIGEST.fullmatch(checksum)
            ):
                errors.append(
                    f"mise.lock {tool}/{platform} lacks verified download metadata"
                )
    return errors


def dockerfile_inputs(content: str) -> list[list[str]]:
    """Read image/ARG instructions, excluding Docker heredoc payloads."""
    instructions: list[list[str]] = []
    heredocs: list[tuple[str, bool]] = []
    continued = ""
    for line in content.splitlines():
        if heredocs:
            delimiter, strip_tabs = heredocs[0]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                heredocs.pop(0)
            continue
        if line.lstrip().startswith("#"):
            continue
        if line.rstrip().endswith("\\"):
            continued += line.rstrip()[:-1] + " "
            continue
        line = continued + line
        continued = ""
        match = re.match(r"^\s*(ARG|FROM|COPY|RUN)\s", line, re.IGNORECASE)
        if match is None:
            continue
        instruction = match[1].upper()
        if instruction in {"COPY", "RUN"}:
            # Non-POSIX lexing retains quotes: a quoted literal '<<EOF' is not
            # a heredoc, but <<'EOF' is. Native Docker heredocs use attached labels.
            lexer = shlex.shlex(line, posix=False)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            for token in lexer:
                delimiter_match = re.fullmatch(
                    r"<<(-?)(?:'([^']+)'|\"([^\"]+)\"|([^\s'\"]+))", token
                )
                if delimiter_match is not None:
                    delimiter = next(
                        group
                        for group in delimiter_match.groups()[1:]
                        if group is not None
                    )
                    heredocs.append((delimiter, delimiter_match[1] == "-"))
        if instruction != "RUN":
            instructions.append(shlex.split(line, comments=True))
    if heredocs:
        raise ToolchainError("unterminated Dockerfile heredoc")
    return instructions


def check_build_inputs(root: Path, manifest: dict[str, object]) -> list[str]:
    errors: list[str] = []
    inputs = build_args(manifest)
    for dockerfile in sorted((root / "images").glob("*/Dockerfile")):
        instructions = dockerfile_inputs(dockerfile.read_text())
        all_stages = {
            tokens[-1].casefold()
            for tokens in instructions
            if tokens[0].upper() == "FROM"
            and len(tokens) >= 4
            and tokens[-2].upper() == "AS"
        }
        previous_stages: set[str] = set()
        # Check actual ARG/FROM/COPY inputs, not comments, stage names or payloads.
        # Distroless is a base projection, independent of its replaced Node binary.
        for tokens in instructions:
            instruction = tokens[0].upper()
            if instruction == "ARG" and len(tokens) > 1:
                key, separator, value = tokens[1].partition("=")
                if separator and key in inputs and value != inputs[key]:
                    errors.append(
                        f"{dockerfile.relative_to(root)}: {key} default differs from root build input"
                    )
            references: list[str] = []
            if instruction == "FROM":
                references = [
                    token for token in tokens[1:] if not token.startswith("--")
                ][:1]
            elif instruction == "COPY":
                references = [
                    token.removeprefix("--from=")
                    for token in tokens[1:]
                    if token.startswith("--from=")
                ]
            for reference in references:
                stages = previous_stages if instruction == "FROM" else all_stages
                if reference.casefold() in stages:
                    continue
                for image_name, pattern in (
                    ("uv", r"^ghcr\.io/astral-sh/uv(?=[:@]|$)"),
                    ("node", r"^node(?=[:@]|$)"),
                    ("pnpm", r"^ghcr\.io/pnpm/pnpm(?=[:@]|$)"),
                    (
                        "node_runtime",
                        r"^gcr\.io/distroless/nodejs\d+-debian\d+(?=[:@]|$)",
                    ),
                ):
                    if re.match(pattern, reference) and reference != image(
                        manifest, image_name
                    ):
                        errors.append(
                            f"{dockerfile.relative_to(root)}: static {image_name} image differs from root projection"
                        )
            if (
                instruction == "FROM"
                and len(tokens) >= 4
                and tokens[-2].upper() == "AS"
            ):
                previous_stages.add(tokens[-1].casefold())
    return errors


def check_environment(root: Path) -> list[str]:
    errors: list[str] = []
    # Exercise discovery from both entrypoints, not only inherited constants.
    for directory in (root, root / "frontend"):
        result = subprocess.run(
            [str(root / "scripts/mise.sh"), "config", "--json"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
        )
        configs = json.loads(result.stdout)
        if (
            not isinstance(configs, list)
            or len(configs) != 1
            or table(configs[0], "loaded config").get("path") != str(root / "mise.toml")
        ):
            errors.append(
                f"mise does not load only root authority from {directory.relative_to(root)}"
            )
        result = subprocess.run(
            [str(root / "scripts/mise.sh"), "env", "--json"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
        )
        env = table(json.loads(result.stdout), "mise environment")
        for key, suffix in CACHE_PATHS.items():
            if env.get(key) != str(root / ".cache") + suffix:
                errors.append(
                    f"{key} changes when executed from {directory.relative_to(root)}"
                )
        interpreter = text(env.get("UV_PYTHON"), "UV_PYTHON")
        if str(root / ".cache") in interpreter:
            errors.append("UV_PYTHON points inside disposable cache storage")
        if env.get("UV_PROJECT_ENVIRONMENT") != str(root / ".venv"):
            errors.append("UV_PROJECT_ENVIRONMENT does not preserve root .venv")
    return errors


def check_selected_versions(root: Path, manifest: dict[str, object]) -> list[str]:
    """Probe actual executables from both supported working directories."""
    commands = {
        "python": [
            "uv",
            "run",
            "--no-project",
            "python",
            "-I",
            "-c",
            "import platform; print(platform.python_version())",
        ],
        "node": ["node", "--version"],
        "uv": ["uv", "--version"],
        "pnpm": ["pnpm", "--version"],
        "prek": ["prek", "--version"],
        "helm": ["helm", "version", "--short"],
        "kind": ["kind", "version"],
        "kubectl": ["kubectl", "version", "--client", "-o", "json"],
        "jq": ["jq", "--version"],
        "kubeconform": ["kubeconform", "-v"],
        "trivy": ["trivy", "--version"],
        "hadolint": ["hadolint", "--version"],
    }
    errors: list[str] = []
    for directory in (root, root / "frontend"):
        for tool, command in commands.items():
            result = subprocess.run(
                [str(root / "scripts/mise.sh"), "exec", "--", *command],
                cwd=directory,
                capture_output=True,
                text=True,
                check=True,
            )
            actual = re.search(r"(?<![0-9])(\d+\.\d+\.\d+)(?![0-9])", result.stdout)
            if actual is None or actual[1] != version(manifest, tool):
                errors.append(
                    f"{tool} executable differs from root pin in {directory.relative_to(root)}"
                )
    return errors


def project(root: Path, manifest: dict[str, object]) -> None:
    """Only the Python-native projection is owned here; frontend metadata is checked."""
    (root / ".python-version").write_text(f"{version(manifest, 'python')}\n")


def hook_environment_prefix() -> str:
    # Native mise-generated shell output is trusted only through our verified root
    # launcher. No user file is sourced, and Prek's original/legacy logic remains.
    return (
        HOOK_BEGIN
        + 'PLATFORM_HOOK_ROOT="$(git rev-parse --show-toplevel)" || exit 1\n'
        + '[ -x "$PLATFORM_HOOK_ROOT/scripts/mise.sh" ] || exit 1\n'
        + 'PLATFORM_HOOK_ENV="$("$PLATFORM_HOOK_ROOT/scripts/mise.sh" env --shell bash)" || exit 1\n'
        + 'eval "$PLATFORM_HOOK_ENV" || exit 1\n'
        + "unset PLATFORM_HOOK_ENV PLATFORM_HOOK_ROOT\n"
        + HOOK_END
    )


def patch_hook(path: Path) -> None:
    content = path.read_text()
    if "# File generated by prek" not in content or not content.startswith("#!/"):
        raise ToolchainError(f"refusing to patch an unrecognized Prek shim: {path}")
    if HOOK_BEGIN in content:
        start = content.index(HOOK_BEGIN)
        end = content.index(HOOK_END, start) + len(HOOK_END)
        content = content[:start] + content[end:]
    first_line, remainder = content.split("\n", 1)
    replacement = first_line + "\n" + hook_environment_prefix() + remainder
    # A GUI Git operation must never observe a truncated or half-written hook.
    mode = path.stat().st_mode
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(replacement)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_hooks(root: Path) -> None:
    """Explicit setup operation, never performed by check or projection commands."""
    subprocess.run(["prek", "install"], cwd=root, check=True)
    for hook in ("pre-commit", "pre-push"):
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", f"hooks/{hook}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        path = Path(result.stdout.strip())
        if not path.is_absolute():
            path = root / path
        patch_hook(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    checker = commands.add_parser(
        "check",
        help="check root metadata, compatibility, locks and subdirectory discovery",
    )
    checker.add_argument(
        "--static", action="store_true", help="omit live mise environment checks"
    )
    commands.add_parser(
        "project", help="regenerate only .python-version from root authority"
    )
    commands.add_parser(
        "install-hooks", help="stock Prek install plus minimal root-cache shim prefix"
    )
    query = commands.add_parser("version", help="print the selected exact version")
    query.add_argument("tool")
    query = commands.add_parser("image", help="print a root-derived image reference")
    query.add_argument("name")
    builder = commands.add_parser(
        "build-args", help="print root-derived Docker build inputs"
    )
    builder.add_argument(
        "--format", choices=("env", "github", "docker", "json"), default="env"
    )
    args = parser.parse_args()
    try:
        manifest = load_toml(ROOT / "mise.toml")
        if args.command == "version":
            print(version(manifest, args.tool))
        elif args.command == "image":
            print(image(manifest, args.name))
        elif args.command == "build-args":
            inputs = build_args(manifest)
            if args.format == "json":
                print(json.dumps(inputs, sort_keys=True))
            else:
                for key, value in inputs.items():
                    prefix = "--build-arg=" if args.format == "docker" else ""
                    print(f"{prefix}{key}={value}")
        elif args.command == "project":
            project(ROOT, manifest)
        elif args.command == "install-hooks":
            install_hooks(ROOT)
        else:
            errors = (
                check_manifest(manifest)
                + check_native_metadata(ROOT, manifest)
                + check_lock(ROOT, manifest)
                + check_build_inputs(ROOT, manifest)
            )
            if not args.static:
                errors.extend(check_environment(ROOT))
                errors.extend(check_selected_versions(ROOT, manifest))
            for error in errors:
                print(f"toolchain: {error}", file=sys.stderr)
            if errors:
                return 1
            print(
                "Root toolchain, native projections and compatibility are consistent."
            )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"toolchain: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
