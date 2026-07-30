import json
import shutil
import subprocess
from pathlib import Path

import pytest

from video_to_skill import sources as source_module
from video_to_skill.config import Settings
from video_to_skill.models import InspectionEntryStatus, SourceKind
from video_to_skill.sources import (
    BilibiliSourceAdapter,
    LocalSourceAdapter,
    SourceRegistry,
    YouTubeSourceAdapter,
    local_source_changed,
    source_cache_identity,
)
from video_to_skill.utils import run_command


def test_platform_host_allowlist() -> None:
    youtube = YouTubeSourceAdapter()
    bilibili = BilibiliSourceAdapter()
    assert youtube.accepts("https://www.youtube.com/watch?v=abc")
    assert youtube.accepts("https://youtu.be/abc")
    assert not youtube.accepts("https://youtube.com.example.org/watch?v=abc")
    assert bilibili.accepts("https://www.bilibili.com/video/BV123")
    assert bilibili.accepts("https://b23.tv/abc")
    assert not bilibili.accepts("file:///tmp/demo.mp4")


def test_registry_rejects_generic_web_urls() -> None:
    registry = SourceRegistry()
    try:
        registry.adapter_for("https://example.com/video.mp4")
    except Exception as exc:
        assert "Unsupported URL host" in str(exc)
    else:
        raise AssertionError("generic URL should not be accepted")


def test_ytdlp_playlist_metadata_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "title": "Course",
        "entries": [
            {
                "id": "one",
                "title": "Lesson One",
                "duration": 60,
                "webpage_url": "https://youtu.be/one",
                "channel": "Teacher",
                "chapters": [{"title": "Intro", "start_time": 0, "end_time": 30}],
                "subtitles": {"en": [{"ext": "vtt", "url": "https://captions/one"}]},
                "formats": [{"height": 720, "filesize_approx": 12345}],
            },
            {
                "id": "two",
                "title": "Lesson Two",
                "duration": 120,
                "webpage_url": "https://youtu.be/two",
            },
        ],
    }

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["yt-dlp"], returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(source_module, "run_command", fake_run)
    adapter = YouTubeSourceAdapter()
    inspected = adapter.inspect(
        "https://youtube.com/playlist?list=course",
        Settings(yt_dlp="python3"),
    )
    assert [item.playlist_index for item in inspected] == [1, 2]
    assert all(item.kind == SourceKind.COURSE for item in inspected)
    assert inspected[0].creator == "Teacher"
    assert inspected[0].metadata["estimated_media_bytes"] == 12345
    assert not inspected[0].captions[0].automatic


def test_ytdlp_playlist_inspection_retains_inaccessible_and_malformed_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "title": "Incomplete Course",
        "playlist_count": 5,
        "entries": [
            {
                "id": "one",
                "title": "Lesson One",
                "webpage_url": "https://youtu.be/one",
            },
            None,
            "malformed",
            {"title": "Private lesson", "availability": "private"},
        ],
    }
    monkeypatch.setattr(
        source_module,
        "run_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["yt-dlp"], returncode=0, stdout=json.dumps(payload), stderr=""
        ),
    )

    inspected = YouTubeSourceAdapter().inspect_with_completeness(
        "https://youtube.com/playlist?list=incomplete",
        Settings(yt_dlp="python3"),
    )

    assert [item.id for item in inspected.sources] == ["youtube-one"]
    report = inspected.completeness
    assert report.expected_entries == 5
    assert report.accessible_entries == 1
    assert report.inaccessible_entries == 2
    assert report.failed_entries == 2
    assert not report.completeness_proven
    assert [entry.status for entry in report.entries] == [
        InspectionEntryStatus.ACCESSIBLE,
        InspectionEntryStatus.INACCESSIBLE,
        InspectionEntryStatus.FAILED,
        InspectionEntryStatus.INACCESSIBLE,
        InspectionEntryStatus.FAILED,
    ]
    assert all(
        entry.reason for entry in report.entries if entry.status != InspectionEntryStatus.ACCESSIBLE
    )


def test_course_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "title": "Course",
        "entries": [
            {"id": "one", "title": "One"},
            {"id": "two", "title": "Two"},
        ],
    }

    monkeypatch.setattr(
        source_module,
        "run_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["yt-dlp"], returncode=0, stdout=json.dumps(payload), stderr=""
        ),
    )
    with pytest.raises(Exception, match="exceeding configured limit"):
        YouTubeSourceAdapter().inspect(
            "https://youtube.com/playlist?list=course",
            Settings(yt_dlp="python3", max_course_items=1),
        )


def test_cache_identity_ignores_expiring_caption_urls() -> None:
    base = {
        "id": "video",
        "title": "Video",
        "webpage_url": "https://youtu.be/video",
        "subtitles": {"en": [{"ext": "vtt", "url": "https://captions?signature=old"}]},
    }
    changed = json.loads(json.dumps(base))
    changed["subtitles"]["en"][0]["url"] = "https://captions?signature=new"
    first = source_module._source_from_ytdlp(
        base,
        platform=source_module.SourcePlatform.YOUTUBE,
        fallback_locator="https://youtu.be/video",
    )
    second = source_module._source_from_ytdlp(
        changed,
        platform=source_module.SourcePlatform.YOUTUBE,
        fallback_locator="https://youtu.be/video",
    )
    assert source_cache_identity(first) == source_cache_identity(second)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg unavailable")
def test_local_sidecar_language_and_change_detection(tmp_path: Path) -> None:
    media = tmp_path / "lesson.mp4"
    caption = tmp_path / "lesson.zh.vtt"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ],
        timeout=30,
    )
    caption.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n你好\n",
        encoding="utf-8",
    )
    source = LocalSourceAdapter().inspect(str(media), Settings())[0]
    assert source.captions[0].language == "zh"
    assert not local_source_changed(source)
    caption.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n你好, 世界\n",
        encoding="utf-8",
    )
    assert local_source_changed(source)
