"""Deterministically materialize evidence-grounded teaching visuals."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from video_to_skill.errors import ProcessingError
from video_to_skill.generation import (
    MAX_ASSET_DIMENSION,
    MAX_ASSET_INPUT_BYTES,
    MAX_ASSET_PIXELS,
    NormalizedCrop,
    VisualAssetCandidate,
    VisualAssetPresentation,
)
from video_to_skill.models import VisualEvent
from video_to_skill.utils import hash_file, is_within
from video_to_skill.workspace import Workspace

_SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
_SEQUENCE_GAP = 12
_SEQUENCE_PANEL_HEIGHT = 720


class MaterializedVisualAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    image_record_id: str
    source_id: str
    evidence_ids: list[str]
    semantic_unit_ids: list[str]
    presentation: VisualAssetPresentation
    crop: NormalizedCrop | None = None
    description: str
    teaching_value: str
    timestamps: list[float] = Field(min_length=1, max_length=4)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _resolve_event_path(workspace: Workspace, event: VisualEvent) -> Path:
    root = workspace.root.resolve()
    raw = event.path.expanduser()
    lexical = raw if raw.is_absolute() else root / raw
    if lexical.is_symlink():
        raise ProcessingError(f"Visual asset source cannot use a symbolic link: {event.path}")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ProcessingError(f"Visual asset source is unavailable: {event.path}") from exc
    if not is_within(resolved, root) or not resolved.is_file():
        raise ProcessingError(f"Visual asset source escapes the evidence workspace: {event.path}")
    if resolved.stat().st_size > MAX_ASSET_INPUT_BYTES:
        raise ProcessingError(f"Visual asset source exceeds the input byte bound: {event.path}")
    return resolved


def _load_frame(workspace: Workspace, event: VisualEvent) -> Image.Image:
    source = _resolve_event_path(workspace, event)
    try:
        with Image.open(source) as image:
            width, height = image.size
            if (
                width < 1
                or height < 1
                or width > MAX_ASSET_DIMENSION
                or height > MAX_ASSET_DIMENSION
                or width * height > MAX_ASSET_PIXELS
            ):
                raise ProcessingError(
                    f"Visual asset source dimensions exceed the safe bound: {width}x{height}"
                )
            if image.format not in _SUPPORTED_IMAGE_FORMATS:
                raise ProcessingError(
                    f"Visual asset source format is unsupported: {image.format or '<unknown>'}"
                )
            if getattr(image, "n_frames", 1) != 1:
                raise ProcessingError("Animated images cannot become teaching visuals")
            image.load()
            return image.convert("RGB")
    except ProcessingError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise ProcessingError(f"Could not decode visual asset source {event.path}: {exc}") from exc


def _crop(image: Image.Image, crop: NormalizedCrop | None) -> Image.Image:
    if crop is None:
        return image
    width, height = image.size
    left = min(width - 1, max(0, round(crop.left * width)))
    top = min(height - 1, max(0, round(crop.top * height)))
    right = min(width, max(left + 1, round(crop.right * width)))
    bottom = min(height, max(top + 1, round(crop.bottom * height)))
    result = image.crop((left, top, right, bottom))
    image.close()
    return result


def _sequence(frames: list[Image.Image]) -> Image.Image:
    panel_width = max(
        1,
        (MAX_ASSET_DIMENSION - _SEQUENCE_GAP * (len(frames) - 1)) // len(frames),
    )
    for frame in frames:
        frame.thumbnail(
            (panel_width, _SEQUENCE_PANEL_HEIGHT),
            Image.Resampling.LANCZOS,
        )
    width = sum(frame.width for frame in frames) + _SEQUENCE_GAP * (len(frames) - 1)
    height = max(frame.height for frame in frames)
    canvas = Image.new("RGB", (width, height), "white")
    offset = 0
    for frame in frames:
        canvas.paste(frame, (offset, (height - frame.height) // 2))
        offset += frame.width + _SEQUENCE_GAP
        frame.close()
    return canvas


def materialize_visual_asset_candidates(
    workspace: Workspace,
    candidates: list[VisualAssetCandidate],
    output_directory: Path,
    *,
    record_prefix: str,
) -> list[tuple[MaterializedVisualAsset, Path]]:
    """Render bounded frame, crop, or sequence candidates into task-owned PNG files."""

    output = output_directory.resolve()
    output.mkdir(parents=True, exist_ok=True)
    events_by_source = {
        source.id: {event.id: event for event in workspace.visuals(source.id)}
        for source in workspace.list_sources()
    }
    materialized: list[tuple[MaterializedVisualAsset, Path]] = []
    for candidate in candidates:
        source_events = events_by_source.get(candidate.source_id, {})
        try:
            events = [source_events[evidence_id] for evidence_id in candidate.evidence_ids]
        except KeyError as exc:
            raise ProcessingError(
                f"Visual asset candidate {candidate.id} references a non-visual evidence ID"
            ) from exc
        frames = [_crop(_load_frame(workspace, event), candidate.crop) for event in events]
        rendered = _sequence(frames) if candidate.presentation == "sequence" else frames[0]
        destination = output / f"{candidate.id}.png"
        try:
            rendered.save(
                destination,
                format="PNG",
                optimize=False,
                compress_level=9,
            )
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise ProcessingError(
                f"Could not materialize visual asset candidate {candidate.id}: {exc}"
            ) from exc
        finally:
            rendered.close()
        with Image.open(destination) as image:
            width, height = image.size
        materialized.append(
            (
                MaterializedVisualAsset(
                    candidate_id=candidate.id,
                    image_record_id=f"{record_prefix}:{candidate.id}",
                    source_id=candidate.source_id,
                    evidence_ids=candidate.evidence_ids,
                    semantic_unit_ids=candidate.semantic_unit_ids,
                    presentation=candidate.presentation,
                    crop=candidate.crop,
                    description=candidate.description,
                    teaching_value=candidate.teaching_value,
                    timestamps=[event.timestamp for event in events],
                    width=width,
                    height=height,
                    sha256=hash_file(destination),
                ),
                destination,
            )
        )
    return materialized
