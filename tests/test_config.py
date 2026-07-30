from pathlib import Path

import pytest

from video_to_skill import config as config_module
from video_to_skill.config import Settings, load_settings


def test_hosted_vision_is_opt_in() -> None:
    assert Settings().vision_provider == "none"


def test_configuration_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "user_config_path", lambda _name: tmp_path / "user")
    project = tmp_path / "project"
    project.mkdir()
    (project / "video-to-skill.toml").write_text(
        """[video_to_skill]
language = "zh"
max_workers = 1
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("VIDEO_TO_SKILL_MAX_WORKERS", "3")
    settings = load_settings(language="en")
    assert settings.language == "en"
    assert settings.max_workers == 3


def test_invalid_configuration_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "user_config_path", lambda _name: tmp_path / "user")
    config = tmp_path / "bad.toml"
    config.write_text("[video_to_skill\n", encoding="utf-8")
    with pytest.raises(Exception, match="Cannot read configuration"):
        load_settings(config)
