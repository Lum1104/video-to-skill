"""Install generator and generated skills into supported agent hosts."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Final

from video_to_skill import __version__
from video_to_skill.errors import ProcessingError
from video_to_skill.utils import atomic_write_text, hash_file

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_GENERATOR_NAME: Final = "video-to-skill"
_GENERATOR_POSIX_LAUNCHER: Final = Path("scripts/video-to-skill")
_GENERATOR_WINDOWS_LAUNCHER: Final = Path("scripts/video-to-skill.cmd")
_GENERATOR_ENGINE: Final = Path("scripts/engine.py")
_GENERATOR_RUNTIME: Final = Path("runtime.json")


class SkillHost(StrEnum):
    """Agent hosts with native local Skill discovery."""

    CLAUDE = "claude"
    CODEX = "codex"


def host_skill_root(
    host: SkillHost,
    *,
    project: bool = False,
    project_root: Path | None = None,
    user_home: Path | None = None,
) -> Path:
    """Return the documented native Skill root for a host and scope."""

    if project:
        base = (project_root or Path.cwd()).expanduser().resolve()
        return base / (".claude/skills" if host == SkillHost.CLAUDE else ".agents/skills")
    base = (user_home or Path.home()).expanduser().resolve()
    return base / (".claude/skills" if host == SkillHost.CLAUDE else ".agents/skills")


def _skill_name(skill_directory: Path) -> str:
    skill_path = skill_directory / "SKILL.md"
    if not skill_path.is_file():
        raise ProcessingError(f"Generated skill has no SKILL.md: {skill_directory}")
    text = skill_path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        raise ProcessingError(f"Generated skill has no YAML frontmatter: {skill_path}")
    end = text.find("\n---", 4)
    if end < 0:
        raise ProcessingError(f"Generated skill has unterminated YAML frontmatter: {skill_path}")
    name = ""
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "name":
            name = value.strip().strip("\"'")
            break
    if not _SKILL_NAME_RE.fullmatch(name) or len(name) > 64:
        raise ProcessingError(
            "Generated skill frontmatter name must use lowercase letters, digits, and "
            "hyphens and be at most 64 characters"
        )
    return name


def _tree_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ProcessingError(f"Skill packages cannot contain symbolic links: {path}")
        if path.is_file():
            manifest[path.relative_to(root).as_posix()] = hash_file(path)
    return manifest


def install_generated_skill(
    skill_directory: Path,
    skill_root: Path,
) -> tuple[Path, str]:
    """Atomically install a validated generated Skill without overwriting conflicts."""

    source = skill_directory.expanduser().resolve()
    if not source.is_dir():
        raise ProcessingError(f"Generated skill directory does not exist: {source}")
    name = _skill_name(source)
    source_manifest = _tree_manifest(source)
    if not source_manifest:
        raise ProcessingError(f"Generated skill directory is empty: {source}")

    root = skill_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    if target.is_symlink():
        raise ProcessingError(f"Refusing to replace symbolic-link target: {target}")
    if target.exists():
        if not target.is_dir():
            raise ProcessingError(f"Skill installation target is not a directory: {target}")
        if _tree_manifest(target) == source_manifest:
            return target, "unchanged"
        raise ProcessingError(
            f"A different skill named '{name}' already exists at {target}. "
            "Keep the generated artifact and choose a different name or explicitly update "
            "the existing skill."
        )

    staging_root = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=root))
    staged = staging_root / name
    try:
        shutil.copytree(source, staged)
        os.replace(staged, target)
    except OSError as exc:
        raise ProcessingError(f"Could not install generated skill at {target}: {exc}") from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return target, "installed"


def bundled_skill_text() -> str:
    packaged = files("video_to_skill").joinpath("bundled", "SKILL.md")
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    repository_copy = Path(__file__).resolve().parents[2] / "SKILL.md"
    if repository_copy.is_file():
        return repository_copy.read_text(encoding="utf-8")
    raise ProcessingError("The bundled video-to-skill SKILL.md could not be located")


def bundled_engine_text() -> str:
    """Return the portable Python entry asset shipped with the generator bundle."""

    packaged = files("video_to_skill").joinpath("bundled", "engine.py")
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    repository_copy = Path(__file__).resolve().parents[2] / "scripts" / "video_to_skill.py"
    if repository_copy.is_file():
        return repository_copy.read_text(encoding="utf-8")
    raise ProcessingError("The bundled video-to-skill engine entry could not be located")


def _bound_python_executable() -> Path:
    if not sys.executable:
        raise ProcessingError("Cannot install the generator bundle without a Python executable")
    executable = Path(os.path.abspath(Path(sys.executable).expanduser()))
    if not executable.is_file():
        raise ProcessingError(f"Generator Python executable is not a file: {executable}")
    return executable


def _posix_launcher(python_executable: Path) -> str:
    return (
        "#!/bin/sh\n"
        "set -eu\n\n"
        'script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)\n'
        f"exec {shlex.quote(str(python_executable))} "
        '"$script_dir/engine.py" "$@"\n'
    )


def _windows_launcher(python_executable: Path) -> str:
    executable = str(python_executable)
    if '"' in executable:
        raise ProcessingError("Generator Python path cannot contain a double quote")
    executable = executable.replace("%", "%%")
    return f'@echo off\nsetlocal\n"{executable}" "%~dp0engine.py" %*\n'


def _generator_bundle_files() -> dict[Path, str]:
    python_executable = _bound_python_executable()
    runtime = {
        "schema_version": 1,
        "engine": "video_to_skill",
        "engine_entry": _GENERATOR_ENGINE.as_posix(),
        "engine_version": __version__,
        "python_executable": str(python_executable),
        "launchers": {
            "posix": _GENERATOR_POSIX_LAUNCHER.as_posix(),
            "windows": _GENERATOR_WINDOWS_LAUNCHER.as_posix(),
        },
    }
    return {
        Path("SKILL.md"): bundled_skill_text(),
        _GENERATOR_ENGINE: bundled_engine_text(),
        _GENERATOR_POSIX_LAUNCHER: _posix_launcher(python_executable),
        _GENERATOR_WINDOWS_LAUNCHER: _windows_launcher(python_executable),
        _GENERATOR_RUNTIME: json.dumps(
            runtime,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    }


def _write_generator_bundle(root: Path, contents: dict[Path, str]) -> None:
    for relative, content in contents.items():
        target = root / relative
        atomic_write_text(target, content)
        if relative == _GENERATOR_POSIX_LAUNCHER:
            target.chmod(0o755)


def _generator_bundle_matches(root: Path, contents: dict[Path, str]) -> bool:
    for relative, content in contents.items():
        target = root / relative
        if target.is_symlink() or not target.is_file():
            return False
        try:
            if target.read_text(encoding="utf-8") != content:
                return False
        except OSError:
            return False
    posix_launcher = root / _GENERATOR_POSIX_LAUNCHER
    return os.name == "nt" or os.access(posix_launcher, os.X_OK)


def install_generator_skill(skill_root: Path, *, overwrite: bool = False) -> tuple[Path, str]:
    """Install an invocation-complete generator bundle without clobbering conflicts."""

    root = skill_root.expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProcessingError(f"Could not create generator Skill root {root}: {exc}") from exc
    target_directory = root / _GENERATOR_NAME
    skill_path = target_directory / "SKILL.md"
    contents = _generator_bundle_files()

    if target_directory.is_symlink():
        raise ProcessingError(
            f"Refusing to replace symbolic-link generator target: {target_directory}"
        )
    if target_directory.exists():
        if not target_directory.is_dir():
            raise ProcessingError(
                f"Generator installation target is not a directory: {target_directory}"
            )
        _tree_manifest(target_directory)
        if _generator_bundle_matches(target_directory, contents):
            return skill_path, "unchanged"
        if not overwrite:
            raise ProcessingError(
                f"A different generator skill or runtime bundle already exists at "
                f"{target_directory}. Use --force to update the managed bundle files."
            )
        try:
            _write_generator_bundle(target_directory, contents)
        except OSError as exc:
            raise ProcessingError(
                f"Could not update generator bundle at {target_directory}: {exc}"
            ) from exc
        return skill_path, "updated"

    staging_root = Path(tempfile.mkdtemp(prefix=f".{_GENERATOR_NAME}.", dir=root))
    staged = staging_root / _GENERATOR_NAME
    try:
        staged.mkdir()
        _write_generator_bundle(staged, contents)
        os.replace(staged, target_directory)
    except OSError as exc:
        raise ProcessingError(
            f"Could not install generator bundle at {target_directory}: {exc}"
        ) from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return skill_path, "installed"
