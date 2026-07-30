"""Layered, secret-free runtime configuration."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_cache_path, user_config_path
from pydantic import BaseModel, ConfigDict, Field

from video_to_skill.errors import ConfigurationError
from video_to_skill.utils import stable_hash


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_root: Path = Field(default_factory=lambda: user_cache_path("video-to-skill") / "jobs")
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    yt_dlp: str = "yt-dlp"
    cookies_from_browser: str | None = None
    cookies_file: Path | None = None
    language: str = "auto"
    output_language: str = "source"
    visual_profile: Literal["adaptive", "always", "transcript"] = "adaptive"
    ocr_provider: str = "auto"
    vision_provider: str = "none"
    vision_base_url: str = "https://api.openai.com/v1"
    vision_model: str = "gpt-4.1-mini"
    vision_api_key_env: str = "OPENAI_API_KEY"
    asr_provider: str = "auto"
    asr_model: str = "small"
    diarize: bool = False
    scene_threshold: float = Field(default=0.32, ge=0, le=1)
    periodic_frame_interval: int = Field(default=30, ge=5, le=600)
    frame_width: int = Field(default=1280, ge=320, le=3840)
    min_segment_seconds: int = Field(default=45, ge=10, le=600)
    target_segment_seconds: int = Field(default=300, ge=30, le=1800)
    max_segment_seconds: int = Field(default=600, ge=60, le=3600)
    max_source_duration_seconds: int = Field(default=8 * 60 * 60, ge=60)
    max_local_file_bytes: int = Field(default=20 * 1024**3, ge=1024**2)
    max_download_bytes: int = Field(default=20 * 1024**3, ge=1024**2)
    max_course_items: int = Field(default=500, ge=1, le=10_000)
    max_workers: int = Field(default=2, ge=1, le=8)
    command_timeout_seconds: int = Field(default=2 * 60 * 60, ge=10)

    @property
    def configuration_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"cache_root"})
        return stable_hash(payload, length=20)


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Cannot read configuration {path}: {exc}") from exc
    section = data.get("video_to_skill", data)
    if not isinstance(section, dict):
        raise ConfigurationError(f"Configuration {path} must contain a table")
    return section


def load_settings(config_path: Path | None = None, **overrides: Any) -> Settings:
    """Load user, project, explicit-file, environment, then CLI overrides."""

    values: dict[str, Any] = {}
    candidates = [
        user_config_path("video-to-skill") / "config.toml",
        Path.cwd() / "video-to-skill.toml",
    ]
    if config_path is not None:
        candidates.append(config_path)
    for candidate in candidates:
        values = _deep_merge(values, _load_toml(candidate))

    env_map: dict[str, tuple[str, Callable[[str], Any]]] = {
        "VIDEO_TO_SKILL_CACHE_ROOT": ("cache_root", Path),
        "VIDEO_TO_SKILL_LANGUAGE": ("language", str),
        "VIDEO_TO_SKILL_OUTPUT_LANGUAGE": ("output_language", str),
        "VIDEO_TO_SKILL_VISUAL_PROFILE": ("visual_profile", str),
        "VIDEO_TO_SKILL_ASR_MODEL": ("asr_model", str),
        "VIDEO_TO_SKILL_DIARIZE": (
            "diarize",
            lambda raw: raw.casefold() in {"1", "true", "yes", "on"},
        ),
        "VIDEO_TO_SKILL_VISION_PROVIDER": ("vision_provider", str),
        "VIDEO_TO_SKILL_VISION_BASE_URL": ("vision_base_url", str),
        "VIDEO_TO_SKILL_VISION_MODEL": ("vision_model", str),
        "VIDEO_TO_SKILL_VISION_API_KEY_ENV": ("vision_api_key_env", str),
        "VIDEO_TO_SKILL_MAX_WORKERS": ("max_workers", int),
        "VIDEO_TO_SKILL_COOKIES_FROM_BROWSER": ("cookies_from_browser", str),
        "VIDEO_TO_SKILL_COOKIES_FILE": ("cookies_file", Path),
    }
    for env_name, (field, converter) in env_map.items():
        raw = os.getenv(env_name)
        if raw is not None:
            values[field] = converter(raw)

    values.update({key: value for key, value in overrides.items() if value is not None})
    try:
        return Settings.model_validate(values)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
