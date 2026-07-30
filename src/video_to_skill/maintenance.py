"""Recoverable workspace maintenance."""

from __future__ import annotations

import shutil

from video_to_skill.errors import ProcessingError
from video_to_skill.sources import MEDIA_EXTENSIONS
from video_to_skill.utils import is_within
from video_to_skill.workspace import Workspace


def clean_cached_media(workspace: Workspace) -> tuple[int, int]:
    """Remove reproducible media/frame artifacts, retaining evidence metadata."""

    if not is_within(workspace.sources_dir, workspace.root):
        raise ProcessingError("Refusing to clean an unsafe workspace path")
    removed_files = 0
    removed_bytes = 0
    for path in sorted(workspace.sources_dir.rglob("*"), reverse=True):
        if not is_within(path, workspace.sources_dir):
            raise ProcessingError(f"Refusing to remove path outside workspace: {path}")
        if path.is_file() and (
            path.suffix.lower() in MEDIA_EXTENSIONS
            or path.suffix.lower() in {".wav", ".jpg", ".jpeg", ".png"}
            or path.name == "scene-metadata.txt"
        ):
            removed_bytes += path.stat().st_size
            path.unlink()
            removed_files += 1
        elif path.is_dir() and path.name == "frames":
            shutil.rmtree(path)
    return removed_files, removed_bytes
