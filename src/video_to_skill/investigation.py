"""Bounded visual-investigation helpers for contact sheets and dense frame windows."""

from __future__ import annotations

import math
import os
import stat
import tempfile
from collections.abc import Sequence
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from video_to_skill.analysis_depth import (
    effective_source_settings,
    verify_analysis_depth_contract,
)
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.models import VisualEvent, VisualKind, VisualOrigin
from video_to_skill.utils import hash_file, is_within, run_command, stable_hash
from video_to_skill.visual import bounded_frame_scale_filter, difference_hash, hash_distance
from video_to_skill.workspace import Workspace

MAX_CONTACT_SHEET_EVENTS = 100
MAX_CONTACT_SHEET_COLUMNS = 10
MIN_CONTACT_SHEET_TILE_WIDTH = 160
MAX_CONTACT_SHEET_TILE_WIDTH = 800
MAX_CONTACT_SHEET_PIXELS = 32_000_000
MAX_CONTACT_SOURCE_PIXELS = 25_000_000

MIN_WINDOW_FPS = 0.1
MAX_WINDOW_FPS = 30.0
MAX_WINDOW_DURATION_SECONDS = 300.0
MAX_WINDOW_FRAMES = 1_800
MIN_WINDOW_FRAME_WIDTH = 160
MAX_WINDOW_FRAME_WIDTH = 1_920

_SHEET_PADDING = 16
_SHEET_GAP = 12
_SHEET_LABEL_HEIGHT = 48
_SHEET_TITLE_HEIGHT = 40
_TILE_ASPECT_HEIGHT = 9
_TILE_ASPECT_WIDTH = 16
_MAX_TITLE_CHARACTERS = 200
_MAX_LABEL_CHARACTERS = 160


def _clean_text(value: str, *, limit: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip() + "…"


def _timestamp_label(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1_000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle].rstrip() + ellipsis
        if draw.textlength(candidate, font=font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + ellipsis


def _validate_contact_sheet_options(
    events: Sequence[VisualEvent],
    *,
    columns: int,
    tile_width: int,
    title: str | None,
) -> tuple[int, int, int]:
    if not events:
        raise ValueError("contact sheet requires at least one visual event")
    if len(events) > MAX_CONTACT_SHEET_EVENTS:
        raise ValueError(f"contact sheet supports at most {MAX_CONTACT_SHEET_EVENTS} visual events")
    if isinstance(columns, bool) or not 1 <= columns <= MAX_CONTACT_SHEET_COLUMNS:
        raise ValueError(f"columns must be between 1 and {MAX_CONTACT_SHEET_COLUMNS}")
    if (
        isinstance(tile_width, bool)
        or not MIN_CONTACT_SHEET_TILE_WIDTH <= tile_width <= MAX_CONTACT_SHEET_TILE_WIDTH
    ):
        raise ValueError(
            "tile_width must be between "
            f"{MIN_CONTACT_SHEET_TILE_WIDTH} and {MAX_CONTACT_SHEET_TILE_WIDTH}"
        )

    actual_columns = min(columns, len(events))
    rows = math.ceil(len(events) / actual_columns)
    image_height = tile_width * _TILE_ASPECT_HEIGHT // _TILE_ASPECT_WIDTH
    title_height = _SHEET_TITLE_HEIGHT if title else 0
    width = 2 * _SHEET_PADDING + actual_columns * tile_width + (actual_columns - 1) * _SHEET_GAP
    height = (
        2 * _SHEET_PADDING
        + title_height
        + rows * (image_height + _SHEET_LABEL_HEIGHT)
        + (rows - 1) * _SHEET_GAP
    )
    if width * height > MAX_CONTACT_SHEET_PIXELS:
        raise ValueError("contact sheet layout is too large; reduce events, columns, or tile width")
    return width, height, image_height


def _resolve_existing_file(path: Path, *, description: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProcessingError(f"{description} does not exist: {path}") from exc
    if not resolved.is_file():
        raise ProcessingError(f"{description} is not a regular file: {path}")
    return resolved


def _reject_symlink_components(path: Path, *, root: Path, description: str) -> None:
    """Reject existing symlinks between a trusted root and a candidate path."""

    absolute_root = Path(os.path.abspath(root.expanduser()))
    absolute_path = Path(os.path.abspath(path.expanduser()))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as exc:
        raise ProcessingError(f"{description} is outside the evidence workspace: {path}") from exc

    current = absolute_root
    for component in relative.parts:
        current /= component
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ProcessingError(
                f"Cannot inspect {description} component {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(mode):
            raise ProcessingError(f"{description} cannot contain symbolic links: {current}")


def _workspace_frame_destination(workspace: Workspace, source_id: str) -> tuple[Path, Path]:
    """Create and verify the fixed, source-contained dense-frame destination."""

    lexical_source = workspace.sources_dir / source_id
    lexical_destination = lexical_source / "investigation-frames"
    _reject_symlink_components(
        lexical_destination,
        root=workspace.root,
        description="Default frame destination",
    )

    source_directory = workspace.source_directory(source_id)
    if not is_within(source_directory, workspace.root):
        raise ProcessingError("Default frame destination source is outside the evidence workspace")
    _reject_symlink_components(
        source_directory,
        root=workspace.root,
        description="Default frame destination",
    )

    destination = source_directory / "investigation-frames"
    try:
        destination.mkdir(exist_ok=True)
    except OSError as exc:
        raise ProcessingError(
            f"Cannot create default frame destination {destination}: {exc}"
        ) from exc
    _reject_symlink_components(
        destination,
        root=source_directory,
        description="Default frame destination",
    )
    try:
        resolved_destination = destination.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProcessingError(
            f"Cannot resolve default frame destination {destination}: {exc}"
        ) from exc
    if not resolved_destination.is_dir():
        raise ProcessingError(
            f"Default frame destination is not a directory: {resolved_destination}"
        )
    if not is_within(resolved_destination, source_directory):
        raise ProcessingError("Default frame destination must remain inside its source workspace")
    return resolved_destination, source_directory


def _verify_workspace_frame_destination(
    destination: Path,
    *,
    expected: Path,
    containment_root: Path,
) -> None:
    """Recheck the default destination before any filesystem write."""

    _reject_symlink_components(
        destination,
        root=containment_root,
        description="Default frame destination",
    )
    try:
        current = destination.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProcessingError(
            f"Cannot resolve default frame destination {destination}: {exc}"
        ) from exc
    if current != expected or not current.is_dir() or not is_within(current, containment_root):
        raise ProcessingError("Default frame destination must remain inside its source workspace")


def _load_contact_image(path: Path, *, tile_width: int, image_height: int) -> Image.Image:
    resolved = _resolve_existing_file(path, description="Visual image")

    try:
        with Image.open(resolved) as opened:
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_CONTACT_SOURCE_PIXELS:
                raise ProcessingError(
                    f"Visual image dimensions exceed the safety limit: {resolved}"
                )
            opened.load()
            transposed = ImageOps.exif_transpose(opened)
            image = transposed.convert("RGB")
    except ProcessingError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise ProcessingError(f"Cannot read visual image {resolved}: {exc}") from exc

    image.thumbnail((tile_width, image_height), Image.Resampling.LANCZOS)
    return image


def generate_contact_sheet(
    events: Sequence[VisualEvent],
    output_path: Path,
    *,
    title: str | None = None,
    columns: int = 4,
    tile_width: int = 320,
) -> Path:
    """Render chronological ``VisualEvent`` images to a deterministic JPEG contact sheet."""

    ordered_events = sorted(events, key=lambda event: event.timestamp)
    if any(not math.isfinite(event.timestamp) for event in ordered_events):
        raise ValueError("contact sheet event timestamps must be finite")
    clean_title = (
        _clean_text(title, limit=_MAX_TITLE_CHARACTERS) if title and title.strip() else None
    )
    width, height, image_height = _validate_contact_sheet_options(
        ordered_events,
        columns=columns,
        tile_width=tile_width,
        title=clean_title,
    )

    expanded_output = output_path.expanduser()
    output = Path(os.path.abspath(expanded_output))
    if output.is_symlink():
        raise ProcessingError(f"Contact sheet output cannot be a symbolic link: {output}")
    input_paths = {
        _resolve_existing_file(event.path, description="Visual image") for event in ordered_events
    }
    try:
        resolved_output = output.resolve()
    except (OSError, RuntimeError) as exc:
        raise ProcessingError(f"Cannot resolve contact sheet output {output_path}: {exc}") from exc
    if resolved_output in input_paths:
        raise ProcessingError("Contact sheet output cannot overwrite one of its input images")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProcessingError(
            f"Cannot create contact sheet directory {output.parent}: {exc}"
        ) from exc

    sheet = Image.new("RGB", (width, height), (244, 245, 247))
    draw = ImageDraw.Draw(sheet)
    title_font = ImageFont.load_default(size=22)
    label_font = ImageFont.load_default(size=14)
    id_font = ImageFont.load_default(size=12)
    if clean_title:
        fitted_title = _fit_text(draw, clean_title, title_font, width - 2 * _SHEET_PADDING)
        draw.text(
            (_SHEET_PADDING, _SHEET_PADDING),
            fitted_title,
            fill=(25, 29, 38),
            font=title_font,
        )

    actual_columns = min(columns, len(ordered_events))
    content_top = _SHEET_PADDING + (_SHEET_TITLE_HEIGHT if clean_title else 0)
    for index, event in enumerate(ordered_events):
        row, column = divmod(index, actual_columns)
        left = _SHEET_PADDING + column * (tile_width + _SHEET_GAP)
        top = content_top + row * (image_height + _SHEET_LABEL_HEIGHT + _SHEET_GAP)

        draw.rectangle(
            (left, top, left + tile_width - 1, top + image_height - 1),
            fill=(20, 23, 29),
        )
        image = _load_contact_image(
            event.path,
            tile_width=tile_width,
            image_height=image_height,
        )
        image_left = left + (tile_width - image.width) // 2
        image_top = top + (image_height - image.height) // 2
        sheet.paste(image, (image_left, image_top))
        image.close()

        label_top = top + image_height
        draw.rectangle(
            (
                left,
                label_top,
                left + tile_width - 1,
                label_top + _SHEET_LABEL_HEIGHT - 1,
            ),
            fill=(37, 41, 50),
        )
        kind = event.kind.value
        time_and_kind = f"{_timestamp_label(event.timestamp)} · {kind}"
        event_id = _clean_text(event.id, limit=_MAX_LABEL_CHARACTERS) or "(no event id)"
        text_width = tile_width - 16
        draw.text(
            (left + 8, label_top + 5),
            _fit_text(draw, time_and_kind, label_font, text_width),
            fill=(247, 248, 250),
            font=label_font,
        )
        draw.text(
            (left + 8, label_top + 26),
            _fit_text(draw, event_id, id_font, text_width),
            fill=(181, 188, 201),
            font=id_font,
        )

    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".jpg",
        )
        temporary = Path(temporary_name)
        os.close(descriptor)
        descriptor = -1
        sheet.save(
            temporary,
            format="JPEG",
            quality=88,
            subsampling=0,
            optimize=False,
            progressive=False,
            exif=b"",
        )
        os.replace(temporary, output)
    except OSError as exc:
        raise ProcessingError(f"Cannot write contact sheet {output}: {exc}") from exc
    finally:
        sheet.close()
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _validate_window(
    *,
    start: float,
    end: float,
    fps: float,
    frame_width: int,
    hash_threshold: int,
) -> tuple[Decimal, Decimal, Decimal, int]:
    if isinstance(start, bool) or not math.isfinite(start) or start < 0:
        raise ValueError("start must be a finite number greater than or equal to 0")
    if isinstance(end, bool) or not math.isfinite(end) or end <= start:
        raise ValueError("end must be a finite number greater than start")
    if (
        isinstance(fps, bool)
        or not math.isfinite(fps)
        or not MIN_WINDOW_FPS <= fps <= MAX_WINDOW_FPS
    ):
        raise ValueError(f"fps must be between {MIN_WINDOW_FPS} and {MAX_WINDOW_FPS}")
    if (
        isinstance(frame_width, bool)
        or not MIN_WINDOW_FRAME_WIDTH <= frame_width <= MAX_WINDOW_FRAME_WIDTH
    ):
        raise ValueError(
            f"frame_width must be between {MIN_WINDOW_FRAME_WIDTH} and {MAX_WINDOW_FRAME_WIDTH}"
        )
    if isinstance(hash_threshold, bool) or not 0 <= hash_threshold <= 16:
        raise ValueError("hash_threshold must be between 0 and 16")

    start_decimal = Decimal(str(start))
    end_decimal = Decimal(str(end))
    fps_decimal = Decimal(str(fps))
    duration = end_decimal - start_decimal
    if duration > Decimal(str(MAX_WINDOW_DURATION_SECONDS)):
        raise ValueError(f"frame window cannot exceed {MAX_WINDOW_DURATION_SECONDS:g} seconds")
    frame_count = int((duration * fps_decimal).to_integral_value(rounding=ROUND_CEILING))
    if frame_count > MAX_WINDOW_FRAMES:
        raise ValueError(f"frame window cannot exceed {MAX_WINDOW_FRAMES} sampled frames")
    return start_decimal, end_decimal, fps_decimal, frame_count


def _same_file_content(path: Path, *, size: int, digest: str) -> bool:
    try:
        return path.is_file() and path.stat().st_size == size and hash_file(path) == digest
    except OSError:
        return False


def _retain_without_overwrite(
    staged_path: Path,
    output_directory: Path,
    *,
    request_key: str,
    index: int,
) -> Path:
    """Hard-link a staged frame into place without replacing any existing path."""

    digest = hash_file(staged_path)
    size = staged_path.stat().st_size
    stems = [
        f"window-{request_key}-{index:06d}-{digest[:16]}",
        f"window-{request_key}-{index:06d}-{digest}",
    ]
    for stem in stems:
        target = output_directory / f"{stem}.jpg"
        try:
            os.link(staged_path, target)
        except FileExistsError:
            if _same_file_content(target, size=size, digest=digest):
                return target
            continue
        except OSError as exc:
            raise ProcessingError(f"Cannot retain extracted frame {target}: {exc}") from exc
        return target
    raise ProcessingError(
        f"Refusing to overwrite an existing frame path with different content for sample {index}"
    )


def extract_window_frames(
    media_path: Path,
    destination: Path,
    source_id: str,
    settings: Settings,
    *,
    start: float,
    end: float,
    fps: float,
    deduplicate: bool = True,
    hash_threshold: int = 3,
    _containment_root: Path | None = None,
) -> list[VisualEvent]:
    """Extract a bounded ``[start, end)`` window and return absolute-time visual events."""

    start_decimal, end_decimal, fps_decimal, frame_count = _validate_window(
        start=start,
        end=end,
        fps=fps,
        frame_width=settings.frame_width,
        hash_threshold=hash_threshold,
    )
    if not source_id or len(source_id) > 512:
        raise ValueError("source_id must contain between 1 and 512 characters")

    media = _resolve_existing_file(media_path, description="Media file")
    try:
        media_stat = media.stat()
    except OSError as exc:
        raise ProcessingError(f"Cannot inspect media file {media}: {exc}") from exc
    if media_stat.st_size > settings.max_local_file_bytes:
        raise ProcessingError(
            f"Media file exceeds the configured local size limit: {media_stat.st_size} bytes"
        )

    if _containment_root is not None:
        try:
            containment_root = _containment_root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProcessingError(
                f"Cannot resolve frame containment root {_containment_root}: {exc}"
            ) from exc
        _reject_symlink_components(
            destination,
            root=containment_root,
            description="Default frame destination",
        )
        try:
            output_directory = destination.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProcessingError(
                f"Cannot resolve default frame destination {destination}: {exc}"
            ) from exc
        if not is_within(output_directory, containment_root):
            raise ProcessingError(
                "Default frame destination must remain inside its source workspace"
            )
        if not output_directory.is_dir():
            raise ProcessingError(f"Frame destination is not a directory: {output_directory}")
        _verify_workspace_frame_destination(
            destination,
            expected=output_directory,
            containment_root=containment_root,
        )
        staging_parent = containment_root
    else:
        containment_root = None
        try:
            output_directory = destination.expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise ProcessingError(f"Cannot resolve frame destination {destination}: {exc}") from exc
        try:
            output_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProcessingError(
                f"Cannot create frame destination {output_directory}: {exc}"
            ) from exc
        if not output_directory.is_dir():
            raise ProcessingError(f"Frame destination is not a directory: {output_directory}")
        staging_parent = output_directory

    duration = end_decimal - start_decimal
    request_key = stable_hash(
        {
            "media": str(media),
            "media_size": media_stat.st_size,
            "media_mtime_ns": media_stat.st_mtime_ns,
            "source": source_id,
            "start": _decimal_text(start_decimal),
            "end": _decimal_text(end_decimal),
            "fps": _decimal_text(fps_decimal),
            "width": settings.frame_width,
            "deduplicate": deduplicate,
            "hash_threshold": hash_threshold,
        },
        length=20,
    )

    try:
        staging_context = tempfile.TemporaryDirectory(
            dir=staging_parent,
            prefix=f".window-{request_key}-",
        )
    except OSError as exc:
        raise ProcessingError(
            f"Cannot stage extracted frames in {output_directory}: {exc}"
        ) from exc

    events: list[VisualEvent] = []
    with staging_context as staging_name:
        staging = Path(staging_name)
        pattern = staging / "frame-%06d.jpg"
        scale = bounded_frame_scale_filter(settings.frame_width)
        decode_duration = duration + Decimal(1) / fps_decimal
        run_command(
            [
                settings.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-ss",
                _decimal_text(start_decimal),
                "-i",
                str(media),
                "-t",
                _decimal_text(decode_duration),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                (
                    f"trim=duration={_decimal_text(duration)},setpts=PTS-STARTPTS,"
                    f"fps=fps={_decimal_text(fps_decimal)}:start_time=0:round=up,{scale}"
                ),
                "-frames:v",
                str(frame_count),
                "-fps_mode",
                "vfr",
                "-q:v",
                "3",
                "-pix_fmt",
                "yuvj420p",
                "-start_number",
                "0",
                str(pattern),
            ],
            timeout=settings.command_timeout_seconds,
        )

        generated = [staging / f"frame-{index:06d}.jpg" for index in range(frame_count)]
        if any(not path.is_file() for path in generated):
            actual_count = sum(path.is_file() for path in generated)
            raise ProcessingError(
                "FFmpeg could not produce the complete requested frame window "
                f"({actual_count}/{frame_count} frames); check the media duration"
            )

        retained: list[tuple[int, Path, Decimal, str]] = []
        previous_hash: str | None = None
        for index, path in enumerate(generated):
            timestamp = start_decimal + Decimal(index) / fps_decimal
            perceptual_hash = difference_hash(path)
            if (
                deduplicate
                and previous_hash is not None
                and hash_distance(previous_hash, perceptual_hash) <= hash_threshold
            ):
                continue
            retained.append((index, path, timestamp, perceptual_hash))
            previous_hash = perceptual_hash

        for index, staged_path, timestamp, perceptual_hash in retained:
            if containment_root is not None:
                _verify_workspace_frame_destination(
                    destination,
                    expected=output_directory,
                    containment_root=containment_root,
                )
            target = _retain_without_overwrite(
                staged_path,
                output_directory,
                request_key=request_key,
                index=index,
            )
            timestamp_text = _decimal_text(timestamp)
            events.append(
                VisualEvent(
                    id=stable_hash(
                        {
                            "source": source_id,
                            "kind": "investigation-window",
                            "timestamp": timestamp_text,
                        },
                        length=24,
                    ),
                    source_id=source_id,
                    timestamp=float(timestamp),
                    path=target.resolve(),
                    kind=VisualKind.UNKNOWN,
                    origin=VisualOrigin.INVESTIGATION,
                    perceptual_hash=perceptual_hash,
                )
            )
    return events


def generate_workspace_contact_sheet(
    workspace: Workspace,
    source_id: str,
    output_path: Path,
    *,
    section: int | None = None,
    title: str | None = None,
    columns: int = 4,
    tile_width: int = 320,
) -> Path:
    """Generate a source or semantic-section contact sheet from workspace evidence."""

    source = workspace.get_source(source_id)
    if section is None:
        events = workspace.visuals(source_id)
        default_title = source.title
    else:
        if isinstance(section, bool) or section < 1:
            raise ValueError("section must be a positive semantic-section ordinal")
        segment = next(
            (
                candidate
                for candidate in workspace.semantic_segments(source_id)
                if candidate.ordinal == section
            ),
            None,
        )
        if segment is None:
            raise ProcessingError(f"Source {source_id} has no semantic section {section}")
        events = workspace.visuals(source_id, start=segment.start, end=segment.end)
        default_title = f"{source.title} - section {section}: {segment.title}"
    return generate_contact_sheet(
        events,
        output_path,
        title=title if title is not None else default_title,
        columns=columns,
        tile_width=tile_width,
    )


def extract_workspace_window_frames(
    workspace: Workspace,
    source_id: str,
    settings: Settings,
    *,
    start: float,
    end: float,
    fps: float,
    destination: Path | None = None,
    deduplicate: bool = True,
    hash_threshold: int = 3,
) -> list[VisualEvent]:
    """Extract a dense window from a workspace source's materialized local media."""

    workspace.get_source(source_id)
    contract = workspace.analysis_depth_contract()
    if contract is None:
        raise ProcessingError("Frame investigation requires a persisted analysis-depth contract")
    verify_analysis_depth_contract(contract, settings=settings)
    budget = contract.budget
    if not budget.visual_sampling_enabled:
        raise ProcessingError(
            "Frame investigation is disabled by the persisted transcript-only visual profile"
        )
    duration = end - start
    if duration > budget.investigation_window_seconds + 1e-9:
        raise ProcessingError(
            f"Requested frame window is {duration:g}s; {contract.effective.value} depth "
            f"allows at most {budget.investigation_window_seconds}s"
        )
    requested_frames = math.ceil(duration * fps)
    if requested_frames > budget.investigation_max_frames_per_window:
        raise ProcessingError(
            f"Requested frame window would sample {requested_frames} frames; "
            f"{contract.effective.value} depth allows at most "
            f"{budget.investigation_max_frames_per_window}"
        )
    effective_settings = effective_source_settings(
        settings,
        contract,
        source_id=source_id,
    )
    record = workspace.materialization_record(source_id)
    if record is None or not record["media_path"]:
        raise ProcessingError(f"Source {source_id} has no materialized media file")
    if destination is None:
        output_directory, containment_root = _workspace_frame_destination(workspace, source_id)
    else:
        output_directory = destination
        containment_root = None
    return extract_window_frames(
        Path(record["media_path"]),
        output_directory,
        source_id,
        effective_settings,
        start=start,
        end=end,
        fps=fps,
        deduplicate=deduplicate,
        hash_threshold=hash_threshold,
        _containment_root=containment_root,
    )
