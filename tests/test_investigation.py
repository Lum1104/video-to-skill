import shutil
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from video_to_skill import investigation
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.investigation import (
    MAX_CONTACT_SHEET_COLUMNS,
    MAX_CONTACT_SHEET_EVENTS,
    MAX_CONTACT_SHEET_TILE_WIDTH,
    MAX_WINDOW_DURATION_SECONDS,
    MAX_WINDOW_FPS,
    MAX_WINDOW_FRAME_WIDTH,
    MAX_WINDOW_FRAMES,
    extract_window_frames,
    extract_workspace_window_frames,
    generate_contact_sheet,
    generate_workspace_contact_sheet,
)
from video_to_skill.models import (
    SemanticSegment,
    SourceDescriptor,
    SourcePlatform,
    VisualEvent,
    VisualKind,
    VisualOrigin,
)
from video_to_skill.utils import run_command
from video_to_skill.visual import (
    MAX_EXTRACTED_FRAME_HEIGHT,
    MAX_EXTRACTED_FRAME_PIXELS,
)
from video_to_skill.workspace import Workspace


def _visual(
    path: Path,
    *,
    event_id: str,
    timestamp: float,
    kind: VisualKind,
) -> VisualEvent:
    return VisualEvent(
        id=event_id,
        source_id="source",
        timestamp=timestamp,
        path=path,
        kind=kind,
    )


def test_contact_sheet_is_chronological_labeled_sized_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blue = tmp_path / "blue.jpg"
    green = tmp_path / "green.jpg"
    red = tmp_path / "red.jpg"
    Image.new("RGB", (320, 180), "blue").save(blue)
    Image.new("RGB", (320, 180), "green").save(green)
    Image.new("RGB", (320, 180), "red").save(red)
    events = [
        _visual(red, event_id="late-event", timestamp=2.75, kind=VisualKind.UI),
        _visual(blue, event_id="early-event", timestamp=0.25, kind=VisualKind.SCENE),
        _visual(green, event_id="middle-event", timestamp=1.5, kind=VisualKind.CODE),
    ]

    captured_text: list[str] = []
    original_text = ImageDraw.ImageDraw.text

    def record_text(
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        captured_text.append(text)
        original_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
    first = generate_contact_sheet(
        events,
        tmp_path / "first.jpg",
        title="Chronology",
        columns=2,
        tile_width=200,
    )
    second = generate_contact_sheet(
        events,
        tmp_path / "second.jpg",
        title="Chronology",
        columns=2,
        tile_width=200,
    )

    assert first == (tmp_path / "first.jpg").resolve()
    assert first.read_bytes() == second.read_bytes()
    with Image.open(first) as sheet:
        assert sheet.size == (444, 404)
        first_tile = sheet.getpixel((116, 112))
        second_tile = sheet.getpixel((328, 112))
        third_tile = sheet.getpixel((116, 284))
    assert first_tile[2] > first_tile[0] + 100
    assert second_tile[1] > second_tile[0] + 50
    assert third_tile[0] > third_tile[2] + 100

    first_render_text = captured_text[:7]
    assert first_render_text == [
        "Chronology",
        "00:00:00.250 · scene",
        "early-event",
        "00:00:01.500 · code",
        "middle-event",
        "00:00:02.750 · ui",
        "late-event",
    ]


def test_contact_sheet_enforces_hard_limits_and_protects_inputs(tmp_path: Path) -> None:
    image_path = tmp_path / "source.jpg"
    Image.new("RGB", (320, 180), "navy").save(image_path)
    event = _visual(
        image_path,
        event_id="event",
        timestamp=0,
        kind=VisualKind.UNKNOWN,
    )

    with pytest.raises(ValueError, match="at least one"):
        generate_contact_sheet([], tmp_path / "empty.jpg")
    with pytest.raises(ValueError, match="at most"):
        generate_contact_sheet(
            [event] * (MAX_CONTACT_SHEET_EVENTS + 1),
            tmp_path / "many.jpg",
        )
    with pytest.raises(ValueError, match="columns"):
        generate_contact_sheet(
            [event],
            tmp_path / "columns.jpg",
            columns=MAX_CONTACT_SHEET_COLUMNS + 1,
        )
    with pytest.raises(ValueError, match="tile_width"):
        generate_contact_sheet(
            [event],
            tmp_path / "wide.jpg",
            tile_width=MAX_CONTACT_SHEET_TILE_WIDTH + 1,
        )
    with pytest.raises(ValueError, match="too large"):
        generate_contact_sheet(
            [event] * MAX_CONTACT_SHEET_EVENTS,
            tmp_path / "huge.jpg",
            columns=1,
            tile_width=MAX_CONTACT_SHEET_TILE_WIDTH,
        )
    with pytest.raises(ProcessingError, match="cannot overwrite"):
        generate_contact_sheet([event], image_path)
    symlink_target = tmp_path / "unrelated.jpg"
    Image.new("RGB", (20, 20), "orange").save(symlink_target)
    symlink_output = tmp_path / "linked-output.jpg"
    symlink_output.symlink_to(symlink_target)
    with pytest.raises(ProcessingError, match="symbolic link"):
        generate_contact_sheet([event], symlink_output)
    assert symlink_target.is_file()


def test_workspace_contact_sheet_selects_semantic_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    source = SourceDescriptor(
        id="source",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/demo.mp4",
        title="Demo",
    )
    workspace.upsert_sources([source])
    workspace.replace_visuals(
        source.id,
        [
            _visual(tmp_path / "one.jpg", event_id="one", timestamp=0.5, kind=VisualKind.SCENE),
            _visual(tmp_path / "two.jpg", event_id="two", timestamp=2.0, kind=VisualKind.UI),
        ],
    )
    workspace.replace_semantic_segments(
        source.id,
        [
            SemanticSegment(
                id="segment-one",
                source_id=source.id,
                ordinal=1,
                title="First",
                start=0,
                end=1,
            ),
            SemanticSegment(
                id="segment-two",
                source_id=source.id,
                ordinal=2,
                title="Second",
                start=1.1,
                end=3,
            ),
        ],
    )

    captured: dict[str, Any] = {}

    def fake_generate(
        events: list[VisualEvent],
        output_path: Path,
        **kwargs: Any,
    ) -> Path:
        captured["events"] = events
        captured["title"] = kwargs["title"]
        return output_path.resolve()

    monkeypatch.setattr(investigation, "generate_contact_sheet", fake_generate)
    output = generate_workspace_contact_sheet(
        workspace,
        source.id,
        tmp_path / "section.jpg",
        section=2,
    )
    assert output == (tmp_path / "section.jpg").resolve()
    assert [event.id for event in captured["events"]] == ["two"]
    assert captured["title"] == "Demo - section 2: Second"

    with pytest.raises(ProcessingError, match="no semantic section 3"):
        generate_workspace_contact_sheet(
            workspace,
            source.id,
            tmp_path / "missing.jpg",
            section=3,
        )


def test_workspace_window_default_destination_rejects_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    source = SourceDescriptor(
        id="source",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/demo.mp4",
        title="Demo",
    )
    workspace.upsert_sources([source])
    source_directory = workspace.source_directory(source.id)
    media = source_directory / "media.mp4"
    media.write_bytes(b"not decoded")
    workspace.update_materialization(
        source.id,
        content_hash="content",
        source_dir=source_directory,
        media_path=media,
        caption_paths=[],
    )
    workspace.create_analysis_run(settings=Settings(cache_root=tmp_path))

    outside = tmp_path / "outside"
    outside.mkdir()
    (source_directory / "investigation-frames").symlink_to(
        outside,
        target_is_directory=True,
    )

    def should_not_extract(*args: Any, **kwargs: Any) -> list[VisualEvent]:
        raise AssertionError("frame extraction must not run for an unsafe default destination")

    monkeypatch.setattr(investigation, "extract_window_frames", should_not_extract)
    with pytest.raises(ProcessingError, match="symbolic links"):
        extract_workspace_window_frames(
            workspace,
            source.id,
            Settings(cache_root=tmp_path),
            start=0,
            end=1,
            fps=1,
        )
    assert list(outside.iterdir()) == []


def _bounded_window_workspace(
    tmp_path: Path,
    *,
    depth: str,
    visual_profile: str = "adaptive",
) -> tuple[Workspace, Settings]:
    settings = Settings(
        cache_root=tmp_path,
        analysis_depth=depth,
        visual_profile=visual_profile,
    )
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=settings,
    )
    source = SourceDescriptor(
        id="source",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/demo.mp4",
        title="Investigation demo",
        duration=2_000,
    )
    workspace.upsert_sources([source])
    source_directory = workspace.source_directory(source.id)
    media = source_directory / "media.mp4"
    media.write_bytes(b"bounded test media")
    workspace.update_materialization(
        source.id,
        content_hash="content",
        source_dir=source_directory,
        media_path=media,
        caption_paths=[],
    )
    workspace.create_analysis_run(settings=settings)
    return workspace, settings


@pytest.mark.parametrize(
    ("depth", "max_seconds", "max_frames"),
    [
        ("standard", 90, 90),
        ("deep", 180, 360),
        ("archival", 300, 1_200),
    ],
)
def test_workspace_frame_investigation_enforces_persisted_depth_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    depth: str,
    max_seconds: int,
    max_frames: int,
) -> None:
    workspace, settings = _bounded_window_workspace(tmp_path, depth=depth)
    captured: list[tuple[float, float, float]] = []

    def fake_extract(
        _media: Path,
        _destination: Path,
        _source_id: str,
        _settings: Settings,
        **kwargs: Any,
    ) -> list[VisualEvent]:
        captured.append((kwargs["start"], kwargs["end"], kwargs["fps"]))
        return []

    monkeypatch.setattr(investigation, "extract_window_frames", fake_extract)

    extract_workspace_window_frames(
        workspace,
        "source",
        settings,
        start=0,
        end=max_seconds,
        fps=max_frames / max_seconds,
    )
    assert captured == [(0, max_seconds, max_frames / max_seconds)]

    with pytest.raises(ProcessingError, match=f"allows at most {max_seconds}s"):
        extract_workspace_window_frames(
            workspace,
            "source",
            settings,
            start=0,
            end=max_seconds + 1,
            fps=0.1,
        )
    with pytest.raises(ProcessingError, match=f"allows at most {max_frames}"):
        extract_workspace_window_frames(
            workspace,
            "source",
            settings,
            start=0,
            end=max_seconds,
            fps=(max_frames + 1) / max_seconds,
        )
    assert len(captured) == 1


def test_transcript_profile_mechanically_disables_frame_investigation(tmp_path: Path) -> None:
    workspace, settings = _bounded_window_workspace(
        tmp_path,
        depth="archival",
        visual_profile="transcript",
    )

    with pytest.raises(ProcessingError, match="transcript-only"):
        extract_workspace_window_frames(
            workspace,
            "source",
            settings,
            start=0,
            end=1,
            fps=1,
        )


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg unavailable")
def test_exact_window_extraction_has_absolute_timestamps_and_scaled_jpegs(
    tmp_path: Path,
) -> None:
    media = tmp_path / "moving test; safe.mkv"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=10:duration=3",
            "-c:v",
            "ffv1",
            str(media),
        ],
        timeout=30,
    )
    destination = tmp_path / "frames; safe"
    sentinel = destination / "keep.txt"
    destination.mkdir()
    sentinel.write_text("untouched", encoding="utf-8")
    settings = Settings(frame_width=320, command_timeout_seconds=30)

    events = extract_window_frames(
        media,
        destination,
        "source",
        settings,
        start=0.5,
        end=1.6,
        fps=4,
        deduplicate=False,
    )
    assert [event.timestamp for event in events] == pytest.approx([0.5, 0.75, 1.0, 1.25, 1.5])
    assert len({event.id for event in events}) == 5
    assert all(event.kind == VisualKind.UNKNOWN for event in events)
    assert all(event.origin == VisualOrigin.INVESTIGATION for event in events)
    assert all(event.perceptual_hash for event in events)
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    for event in events:
        with Image.open(event.path) as image:
            assert image.format == "JPEG"
            assert image.size == (320, 180)

    repeated = extract_window_frames(
        media,
        destination,
        "source",
        settings,
        start=0.5,
        end=1.6,
        fps=4,
        deduplicate=False,
    )
    assert [event.id for event in repeated] == [event.id for event in events]
    assert [event.path for event in repeated] == [event.path for event in events]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg unavailable")
def test_window_extraction_bounds_extreme_portrait_height_and_pixels(
    tmp_path: Path,
) -> None:
    media = tmp_path / "extreme-portrait.mkv"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=16x4096:d=2:r=2",
            "-c:v",
            "ffv1",
            str(media),
        ],
        timeout=30,
    )

    events = extract_window_frames(
        media,
        tmp_path / "portrait-frames",
        "portrait",
        Settings(frame_width=MAX_WINDOW_FRAME_WIDTH, command_timeout_seconds=30),
        start=0,
        end=1,
        fps=1,
        deduplicate=False,
    )

    assert len(events) == 1
    with Image.open(events[0].path) as image:
        width, height = image.size
    assert width <= MAX_WINDOW_FRAME_WIDTH
    assert height <= MAX_EXTRACTED_FRAME_HEIGHT
    assert width * height <= MAX_EXTRACTED_FRAME_PIXELS


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg unavailable")
def test_window_extraction_deduplicates_adjacent_frames_without_sweeping_destination(
    tmp_path: Path,
) -> None:
    media = tmp_path / "colors.mkv"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:d=1:r=10",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:d=1:r=10",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "ffv1",
            str(media),
        ],
        timeout=30,
    )
    destination = tmp_path / "frames"
    destination.mkdir()
    sentinel = destination / "unrelated.jpg"
    Image.new("RGB", (20, 20), "purple").save(sentinel)

    events = extract_window_frames(
        media,
        destination,
        "source",
        Settings(frame_width=320, command_timeout_seconds=30),
        start=0.5,
        end=1.5,
        fps=4,
    )
    assert [event.timestamp for event in events] == pytest.approx([0.5, 1])
    assert len(events) == 2
    assert sentinel.is_file()


@pytest.mark.parametrize(
    ("start", "end", "fps", "message"),
    [
        (-0.1, 1, 1, "start"),
        (1, 1, 1, "end"),
        (2, 1, 1, "end"),
        (0, 1, 0, "fps"),
        (0, 1, MAX_WINDOW_FPS + 0.1, "fps"),
        (0, MAX_WINDOW_DURATION_SECONDS + 1, 0.1, "cannot exceed"),
        (0, MAX_WINDOW_FRAMES / MAX_WINDOW_FPS + 1, MAX_WINDOW_FPS, "sampled frames"),
    ],
)
def test_window_extraction_validates_bounds_before_touching_media(
    tmp_path: Path,
    start: float,
    end: float,
    fps: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        extract_window_frames(
            tmp_path / "missing.mp4",
            tmp_path / "frames",
            "source",
            Settings(frame_width=320),
            start=start,
            end=end,
            fps=fps,
        )


def test_window_extraction_rejects_excessive_frame_width_before_media_access(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="frame_width"):
        extract_window_frames(
            tmp_path / "missing.mp4",
            tmp_path / "frames",
            "source",
            Settings(frame_width=3_840),
            start=0,
            end=1,
            fps=1,
        )
