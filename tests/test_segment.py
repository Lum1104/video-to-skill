from video_to_skill.config import Settings
from video_to_skill.models import (
    Chapter,
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
    VisualEvent,
    VisualKind,
)
from video_to_skill.segment import segment_timeline


def _transcript(source_id: str, start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        id=f"t-{start}",
        source_id=source_id,
        start=start,
        end=end,
        text=text,
        origin=TranscriptOrigin.MANUAL_CAPTION,
    )


def test_creator_chapters_are_hard_boundaries(tmp_path) -> None:
    source = SourceDescriptor(
        id="youtube-demo",
        platform=SourcePlatform.YOUTUBE,
        locator="https://youtu.be/demo",
        canonical_url="https://youtu.be/demo",
        title="Demo",
        duration=900,
        chapters=[
            Chapter(title="Setup", start=0, end=300),
            Chapter(title="Build", start=300, end=600),
            Chapter(title="Verify", start=600, end=900),
        ],
    )
    transcripts = [
        _transcript(source.id, index * 30, index * 30 + 20, f"topic {index}") for index in range(30)
    ]
    visuals = [
        VisualEvent(
            id="v-1",
            source_id=source.id,
            timestamp=450,
            path=tmp_path / "frame.jpg",
            kind=VisualKind.CODE,
        )
    ]
    settings = Settings(
        min_segment_seconds=45,
        target_segment_seconds=300,
        max_segment_seconds=600,
    )
    segments = segment_timeline(source, transcripts, visuals, settings)
    assert [item.start for item in segments] == [0, 300, 600]
    assert segments[1].title == "Build"
    assert "v-1" in segments[1].visual_event_ids


def test_long_video_gets_maximum_duration_splits() -> None:
    source = SourceDescriptor(
        id="local-demo",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/demo.mp4",
        title="Demo",
        duration=1300,
    )
    transcripts = [
        _transcript(source.id, index * 100, index * 100 + 90, "same subject") for index in range(13)
    ]
    settings = Settings(target_segment_seconds=300, max_segment_seconds=600)
    segments = segment_timeline(source, transcripts, [], settings)
    assert len(segments) >= 3
    assert all(item.end - item.start <= 600.01 for item in segments)
