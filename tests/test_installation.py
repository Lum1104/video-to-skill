import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from video_to_skill.errors import ProcessingError
from video_to_skill.installation import (
    SkillHost,
    bundled_engine_text,
    host_skill_root,
    install_generated_skill,
    install_generator_skill,
)


def _write_generated_skill(root: Path, *, name: str = "course-skill") -> Path:
    root.mkdir()
    (root / "SKILL.md").write_text(
        f"""---
name: {name}
description: Teach and apply the grounded course.
---

# Course
""",
        encoding="utf-8",
    )
    (root / "sources.md").write_text("# Sources\n", encoding="utf-8")
    return root


def test_install_generator_skill_is_idempotent(tmp_path: Path) -> None:
    path, status = install_generator_skill(tmp_path)
    assert status == "installed"
    assert path.is_file()
    assert "name: video-to-skill" in path.read_text(encoding="utf-8")
    bundle = path.parent
    engine = bundle / "scripts/engine.py"
    posix_launcher = bundle / "scripts/video-to-skill"
    windows_launcher = bundle / "scripts/video-to-skill.cmd"
    runtime = json.loads((bundle / "runtime.json").read_text(encoding="utf-8"))
    assert engine.is_file()
    assert posix_launcher.is_file()
    assert windows_launcher.is_file()
    assert posix_launcher.stat().st_mode & stat.S_IXUSR
    assert runtime["python_executable"] == str(Path(sys.executable).absolute())
    assert runtime["engine_entry"] == "scripts/engine.py"
    assert runtime["launchers"] == {
        "posix": "scripts/video-to-skill",
        "windows": "scripts/video-to-skill.cmd",
    }

    command = (
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(windows_launcher), "--version"]
        if os.name == "nt"
        else [str(posix_launcher), "--version"]
    )
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()

    repeated_path, repeated_status = install_generator_skill(tmp_path)
    assert repeated_path == path
    assert repeated_status == "unchanged"


def test_install_refuses_different_existing_skill_without_force(tmp_path: Path) -> None:
    target = tmp_path / "video-to-skill" / "SKILL.md"
    target.parent.mkdir()
    target.write_text("different", encoding="utf-8")
    notes = target.parent / "user-notes.md"
    notes.write_text("preserve", encoding="utf-8")
    with pytest.raises(Exception, match="different generator skill"):
        install_generator_skill(tmp_path)
    assert target.read_text(encoding="utf-8") == "different"
    assert not (target.parent / "scripts/video-to-skill").exists()

    path, status = install_generator_skill(tmp_path, overwrite=True)
    assert path == target
    assert status == "updated"
    assert "name: video-to-skill" in target.read_text(encoding="utf-8")
    assert (target.parent / "scripts/video-to-skill").is_file()
    assert notes.read_text(encoding="utf-8") == "preserve"


def test_install_generator_refuses_to_overwrite_tampered_runtime(tmp_path: Path) -> None:
    path, _ = install_generator_skill(tmp_path)
    launcher = path.parent / "scripts/video-to-skill"
    launcher.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")

    with pytest.raises(ProcessingError, match="different generator skill"):
        install_generator_skill(tmp_path)
    assert launcher.read_text(encoding="utf-8") == "#!/bin/sh\nexit 42\n"


def test_bundled_engine_entry_supports_checkout_and_installed_bundle() -> None:
    content = bundled_engine_text()
    assert "def _load_engine()" in content
    assert 'REPOSITORY_ROOT / "src"' in content
    assert "def _bootstrap_checkout()" in content
    assert "def _prepare_runtime()" in content
    assert "def _probe_imports(" in content
    assert "ensure-capability" in content
    assert "CAPABILITIES =" in content
    assert "VIDEO_TO_SKILL_RUNTIME_ROOT" in content
    assert '"--editable"' in content
    assert "BOOTSTRAP_TIMEOUT_SECONDS" in content


def test_source_launchers_never_reuse_a_copied_checkout_venv() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    posix = (repository_root / "scripts/video-to-skill").read_text(encoding="utf-8")
    windows = (repository_root / "scripts/video-to-skill.cmd").read_text(encoding="utf-8")

    assert ".venv" not in posix
    assert ".venv" not in windows
    assert "VIDEO_TO_SKILL_PYTHON" in posix
    assert "VIDEO_TO_SKILL_PYTHON" in windows


def test_host_skill_roots_follow_native_conventions(tmp_path: Path) -> None:
    project = tmp_path / "project"
    user_home = tmp_path / "user"
    assert (
        host_skill_root(
            SkillHost.CLAUDE,
            project=True,
            project_root=project,
        )
        == project.resolve() / ".claude/skills"
    )
    assert (
        host_skill_root(
            SkillHost.CODEX,
            project=True,
            project_root=project,
        )
        == project.resolve() / ".agents/skills"
    )
    assert (
        host_skill_root(
            SkillHost.CLAUDE,
            user_home=user_home,
        )
        == user_home.resolve() / ".claude/skills"
    )
    assert (
        host_skill_root(
            SkillHost.CODEX,
            user_home=user_home,
        )
        == user_home.resolve() / ".agents/skills"
    )


def test_install_generated_skill_uses_frontmatter_name_and_is_idempotent(
    tmp_path: Path,
) -> None:
    source = _write_generated_skill(tmp_path / "staged-output", name="grounded-course")
    skill_root = tmp_path / "skills"
    target, status = install_generated_skill(source, skill_root)
    assert status == "installed"
    assert target == skill_root.resolve() / "grounded-course"
    assert (target / "sources.md").is_file()

    repeated_target, repeated_status = install_generated_skill(source, skill_root)
    assert repeated_target == target
    assert repeated_status == "unchanged"


def test_install_generated_skill_preserves_conflicting_existing_skill(tmp_path: Path) -> None:
    source = _write_generated_skill(tmp_path / "generated")
    skill_root = tmp_path / "skills"
    target = skill_root / "course-skill"
    target.mkdir(parents=True)
    existing = target / "SKILL.md"
    existing.write_text("user-owned", encoding="utf-8")

    with pytest.raises(ProcessingError, match="different skill"):
        install_generated_skill(source, skill_root)
    assert existing.read_text(encoding="utf-8") == "user-owned"


def test_install_generated_skill_rejects_symlinks(tmp_path: Path) -> None:
    source = _write_generated_skill(tmp_path / "generated")
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (source / "linked.md").symlink_to(outside)

    with pytest.raises(ProcessingError, match="symbolic links"):
        install_generated_skill(source, tmp_path / "skills")
