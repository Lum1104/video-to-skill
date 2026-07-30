import shutil
from pathlib import Path

import pytest
from PIL import Image

from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.utils import run_command
from video_to_skill.visual import (
    MAX_EXTRACTED_FRAME_HEIGHT,
    MAX_EXTRACTED_FRAME_PIXELS,
    MAX_EXTRACTED_FRAME_WIDTH,
    bounded_frame_scale_filter,
    difference_hash,
    extract_candidate_frames,
    hash_distance,
)


def test_color_aware_hash_preserves_distinct_visual_states(tmp_path: Path) -> None:
    red = tmp_path / "red.jpg"
    blue = tmp_path / "blue.jpg"
    red_again = tmp_path / "red-again.jpg"
    Image.new("RGB", (100, 100), "red").save(red)
    Image.new("RGB", (100, 100), "blue").save(blue)
    Image.new("RGB", (100, 100), "red").save(red_again)
    assert hash_distance(difference_hash(red), difference_hash(red_again)) == 0
    assert hash_distance(difference_hash(red), difference_hash(blue)) > 5


def test_difference_hash_rejects_oversized_dimensions_before_conversion(
    tmp_path: Path,
) -> None:
    too_wide = tmp_path / "too-wide.png"
    Image.new("RGB", (MAX_EXTRACTED_FRAME_WIDTH + 1, 1), "red").save(too_wide)

    with pytest.raises(ProcessingError, match="dimensions exceed"):
        difference_hash(too_wide)


def test_scale_filter_bounding_box_has_an_independent_pixel_limit() -> None:
    scale = bounded_frame_scale_filter(MAX_EXTRACTED_FRAME_WIDTH)
    pixel_safe_height = MAX_EXTRACTED_FRAME_PIXELS // MAX_EXTRACTED_FRAME_WIDTH

    assert pixel_safe_height < MAX_EXTRACTED_FRAME_HEIGHT
    assert f"min({MAX_EXTRACTED_FRAME_WIDTH},iw)" in scale
    assert f"min({pixel_safe_height - pixel_safe_height % 2},ih)" in scale
    assert (
        MAX_EXTRACTED_FRAME_WIDTH * (pixel_safe_height - pixel_safe_height % 2)
        <= MAX_EXTRACTED_FRAME_PIXELS
    )


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg unavailable")
def test_ffmpeg_frame_extraction_keeps_scene_change(tmp_path: Path) -> None:
    media = tmp_path / "demo.mp4"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:d=2:r=10",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:d=2:r=10",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ],
        timeout=30,
    )
    settings = Settings(
        periodic_frame_interval=30,
        scene_threshold=0.2,
        command_timeout_seconds=30,
    )
    events = extract_candidate_frames(media, tmp_path / "frames", "source", settings)
    assert len(events) >= 2
    assert events[0].timestamp == 0
    assert any(1.5 <= item.timestamp <= 2.5 for item in events)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg unavailable")
def test_candidate_extraction_bounds_extreme_portrait_height_and_pixels(
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

    events = extract_candidate_frames(
        media,
        tmp_path / "frames",
        "portrait",
        Settings(
            frame_width=MAX_EXTRACTED_FRAME_WIDTH,
            periodic_frame_interval=30,
            command_timeout_seconds=30,
        ),
    )

    assert events
    for event in events:
        with Image.open(event.path) as image:
            width, height = image.size
        assert width <= MAX_EXTRACTED_FRAME_WIDTH
        assert height <= MAX_EXTRACTED_FRAME_HEIGHT
        assert width * height <= MAX_EXTRACTED_FRAME_PIXELS
