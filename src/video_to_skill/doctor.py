"""Environment diagnostics with actionable capability reporting."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass

from video_to_skill.config import Settings


@dataclass(frozen=True)
class Diagnostic:
    capability: str
    status: str
    detail: str
    required: bool = False


def run_diagnostics(settings: Settings) -> list[Diagnostic]:
    diagnostics = [
        Diagnostic(
            "python",
            "ok" if (3, 11) <= sys.version_info[:2] < (3, 14) else "error",
            sys.version.split()[0],
            required=True,
        )
    ]
    for capability, program, required in (
        ("ffmpeg", settings.ffmpeg, True),
        ("ffprobe", settings.ffprobe, True),
        ("yt-dlp", settings.yt_dlp, False),
    ):
        resolved = shutil.which(program)
        diagnostics.append(
            Diagnostic(
                capability,
                "ok" if resolved else ("error" if required else "warning"),
                resolved or f"'{program}' not found on PATH",
                required=required,
            )
        )
    for capability, module, extra in (
        ("local ASR", "faster_whisper", "asr"),
        ("local OCR", "rapidocr", "ocr"),
    ):
        available = importlib.util.find_spec(module) is not None
        diagnostics.append(
            Diagnostic(
                capability,
                "ok" if available else "optional",
                (
                    f"module '{module}' available"
                    if available
                    else f"install `video-to-skill[{extra}]`"
                ),
            )
        )
    vision_key = os.getenv(settings.vision_api_key_env)
    if settings.vision_provider == "none":
        vision_status = "optional"
        vision_detail = (
            "hosted vision disabled; a multimodal agent host can inspect contact sheets natively"
        )
    else:
        vision_status = "ok" if vision_key else "optional"
        vision_detail = (
            f"{settings.vision_model} via {settings.vision_base_url}"
            if vision_key
            else f"set {settings.vision_api_key_env} to enable hosted vision analysis"
        )
    diagnostics.append(Diagnostic("vision", vision_status, vision_detail))
    cache_parent = settings.cache_root.expanduser()
    probe = cache_parent
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
        status = "ok" if free >= 5 * 1024**3 else "warning"
        detail = f"{free / 1024**3:.1f} GiB free at {probe}"
    except OSError as exc:
        status = "warning"
        detail = str(exc)
    diagnostics.append(Diagnostic("workspace storage", status, detail))
    return diagnostics


def diagnostics_ok(diagnostics: list[Diagnostic]) -> bool:
    return not any(item.required and item.status == "error" for item in diagnostics)
