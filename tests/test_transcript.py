from pathlib import Path

from video_to_skill.models import (
    MaterializedSource,
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
)
from video_to_skill.transcript import (
    best_caption,
    caption_quality,
    parse_caption,
    parse_timestamp,
)


def test_parse_timestamps() -> None:
    assert parse_timestamp("01:02.500") == 62.5
    assert parse_timestamp("01:01:02,250") == 3662.25


def test_parse_and_deduplicate_rolling_vtt(tmp_path: Path) -> None:
    path = tmp_path / "captions.en.vtt"
    path.write_text(
        """WEBVTT

00:00:00.000 --> 00:00:02.000
Hello

00:00:01.500 --> 00:00:03.000
Hello world

00:00:04.000 --> 00:00:05.000
<c>Next &amp; final</c>
""",
        encoding="utf-8",
    )
    segments = parse_caption(
        path,
        source_id="source",
        language="en",
        origin=TranscriptOrigin.MANUAL_CAPTION,
    )
    assert [item.text for item in segments] == ["Hello world", "Next & final"]
    assert segments[0].start == 0
    assert segments[0].end == 3


def test_caption_quality_flags_short_long_video(tmp_path: Path) -> None:
    path = tmp_path / "captions.vtt"
    path.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nshort\n",
        encoding="utf-8",
    )
    segments = parse_caption(
        path,
        source_id="source",
        language="en",
        origin=TranscriptOrigin.AUTO_CAPTION,
    )
    score, warnings = caption_quality(segments, duration=600)
    assert score < 0.25
    assert "caption-too-short" in warnings
    assert "caption-low-coverage" in warnings


def test_requested_language_wins_over_filename_order(tmp_path: Path) -> None:
    german = tmp_path / "media.de.vtt"
    english = tmp_path / "media.en.vtt"
    german.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:10.000\nDeutscher Text für den Kurs.\n",
        encoding="utf-8",
    )
    english.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:10.000\nEnglish text for the course.\n",
        encoding="utf-8",
    )
    source = SourceDescriptor(
        id="youtube-demo",
        platform=SourcePlatform.YOUTUBE,
        locator="https://youtu.be/demo",
        title="Demo",
        duration=10,
    )
    materialized = MaterializedSource(
        source=source,
        directory=tmp_path,
        caption_paths=[german, english],
        content_hash="hash",
    )
    segments, selected, _warnings = best_caption(materialized, preferred_language="en")
    assert selected == english
    assert segments[0].text.startswith("English")
