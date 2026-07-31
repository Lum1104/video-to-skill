import json
from pathlib import Path

import pytest

from video_to_skill import authentication as authentication_module
from video_to_skill import pipeline as pipeline_module
from video_to_skill.config import Settings
from video_to_skill.models import (
    AgentObservation,
    CaptionTrack,
    InspectionCompleteness,
    InspectionEntry,
    InspectionEntryStatus,
    MaterializedSource,
    ObservationProducer,
    ObservationType,
    SourceDescriptor,
    SourceInspection,
    SourcePlatform,
    VisualEvent,
    VisualKind,
    VisualOrigin,
)
from video_to_skill.pipeline import extract_sources
from video_to_skill.sources import SourceAdapter, SourceRegistry
from video_to_skill.workspace import Workspace


class FixtureAdapter(SourceAdapter):
    platform = SourcePlatform.YOUTUBE

    def accepts(self, locator: str) -> bool:
        return locator == "fixture://course"

    def inspect(self, locator: str, settings: Settings) -> list[SourceDescriptor]:
        del settings
        return [
            SourceDescriptor(
                id="youtube-fixture",
                platform=self.platform,
                locator=locator,
                canonical_url="https://youtu.be/fixture",
                title="Fixture Course",
                duration=20,
                captions=[CaptionTrack(language="en", extension="vtt", automatic=False)],
            )
        ]

    def materialize(
        self,
        source: SourceDescriptor,
        destination: Path,
        settings: Settings,
        *,
        need_media: bool,
    ) -> MaterializedSource:
        del settings, need_media
        destination.mkdir(parents=True, exist_ok=True)
        caption = destination / "media.en.vtt"
        caption.write_text(
            """WEBVTT

00:00:00.000 --> 00:00:09.000
First we configure the project and verify the inputs.

00:00:10.000 --> 00:00:20.000
Then we run the workflow and inspect the expected result.
""",
            encoding="utf-8",
        )
        return MaterializedSource(
            source=source,
            directory=destination,
            caption_paths=[caption],
            content_hash="fixture-hash",
        )


class AuthenticatedFixtureAdapter(FixtureAdapter):
    def __init__(self) -> None:
        self.cookie_paths: list[Path] = []

    def accepts(self, locator: str) -> bool:
        return locator == "https://youtu.be/private"

    def inspect(self, locator: str, settings: Settings) -> list[SourceDescriptor]:
        self._record_cookie(settings)
        return super().inspect(locator, settings)

    def materialize(
        self,
        source: SourceDescriptor,
        destination: Path,
        settings: Settings,
        *,
        need_media: bool,
    ) -> MaterializedSource:
        self._record_cookie(settings)
        return super().materialize(
            source,
            destination,
            settings,
            need_media=need_media,
        )

    def _record_cookie(self, settings: Settings) -> None:
        assert settings.cookies_from_browser is None
        assert settings.cookies_file is not None
        assert settings.cookies_file.is_file()
        self.cookie_paths.append(settings.cookies_file)


class NoCaptionAdapter(FixtureAdapter):
    platform = SourcePlatform.BILIBILI

    def __init__(self) -> None:
        self.materialized = False

    def accepts(self, locator: str) -> bool:
        return locator == "fixture://no-captions"

    def inspect(self, locator: str, settings: Settings) -> list[SourceDescriptor]:
        del settings
        return [
            SourceDescriptor(
                id="bilibili-no-captions",
                platform=self.platform,
                locator=locator,
                title="No Captions",
                duration=60,
            )
        ]

    def materialize(
        self,
        source: SourceDescriptor,
        destination: Path,
        settings: Settings,
        *,
        need_media: bool,
    ) -> MaterializedSource:
        self.materialized = True
        return super().materialize(source, destination, settings, need_media=need_media)


class PartialCourseAdapter(FixtureAdapter):
    def accepts(self, locator: str) -> bool:
        return locator == "fixture://partial"

    def inspect(self, locator: str, settings: Settings) -> list[SourceDescriptor]:
        del settings
        return [
            SourceDescriptor(
                id="youtube-partial",
                platform=self.platform,
                locator=locator,
                canonical_url="https://youtu.be/partial",
                title="Accessible Lesson",
                duration=20,
                captions=[CaptionTrack(language="en", extension="vtt", automatic=False)],
            )
        ]

    def inspect_with_completeness(
        self,
        locator: str,
        settings: Settings,
    ) -> SourceInspection:
        sources = self.inspect(locator, settings)
        return SourceInspection(
            sources=sources,
            completeness=InspectionCompleteness(
                locator=locator,
                platform=self.platform,
                expected_entries=2,
                accessible_entries=1,
                inaccessible_entries=1,
                failed_entries=0,
                completeness_proven=False,
                disclaimer="One expected lesson is private.",
                entries=[
                    InspectionEntry(
                        ordinal=1,
                        status=InspectionEntryStatus.ACCESSIBLE,
                        source_id=sources[0].id,
                        title=sources[0].title,
                        locator=sources[0].canonical_url,
                    ),
                    InspectionEntry(
                        ordinal=2,
                        status=InspectionEntryStatus.INACCESSIBLE,
                        reason="private video",
                    ),
                ],
            ),
        )


class MutableCourseAdapter(FixtureAdapter):
    def __init__(self) -> None:
        self.source_ids = ["one", "two"]

    def accepts(self, locator: str) -> bool:
        return locator == "fixture://mutable"

    def inspect(self, locator: str, settings: Settings) -> list[SourceDescriptor]:
        del settings
        return [
            SourceDescriptor(
                id=f"youtube-{source_id}",
                platform=self.platform,
                locator=locator,
                canonical_url=f"https://youtu.be/{source_id}",
                title=f"Lesson {source_id}",
                duration=20,
                captions=[CaptionTrack(language="en", extension="vtt", automatic=False)],
            )
            for source_id in self.source_ids
        ]


class UnavailableCourseAdapter(FixtureAdapter):
    def accepts(self, locator: str) -> bool:
        return locator == "fixture://unavailable"

    def inspect_with_completeness(
        self,
        locator: str,
        settings: Settings,
    ) -> SourceInspection:
        del settings
        return SourceInspection(
            sources=[],
            completeness=InspectionCompleteness(
                locator=locator,
                platform=self.platform,
                expected_entries=1,
                accessible_entries=0,
                inaccessible_entries=1,
                failed_entries=0,
                completeness_proven=False,
                disclaimer="The only expected lesson is unavailable.",
                entries=[
                    InspectionEntry(
                        ordinal=1,
                        status=InspectionEntryStatus.INACCESSIBLE,
                        reason="deleted video",
                    )
                ],
            ),
        )


def test_end_to_end_transcript_profile(tmp_path: Path) -> None:
    settings = Settings(
        cache_root=tmp_path,
        visual_profile="transcript",
        asr_provider="none",
        max_workers=1,
    )
    registry = SourceRegistry([FixtureAdapter()])
    workspace, manifest = extract_sources(
        ["fixture://course"],
        settings,
        workspace_path=tmp_path / "workspace",
        registry=registry,
    )
    assert manifest.state.value == "complete"
    assert len(workspace.transcripts("youtube-fixture")) == 2
    assert len(workspace.semantic_segments("youtube-fixture")) == 1
    assert (workspace.root / "coverage.json").is_file()

    # A second run must reuse completed stages without materializing again.
    workspace_again, manifest_again = extract_sources(
        ["fixture://course"],
        settings,
        workspace_path=tmp_path / "workspace",
        registry=registry,
    )
    assert workspace_again.root == workspace.root
    assert manifest_again.state.value == "complete"


def test_extract_reuses_one_browser_cookie_snapshot_for_all_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exports = 0

    monkeypatch.setattr(
        authentication_module,
        "require_program",
        lambda _program: "yt-dlp",
    )

    def fake_export(
        args: list[str],
        **_kwargs: object,
    ) -> object:
        nonlocal exports
        exports += 1
        cookie_path = Path(args[args.index("--cookies") + 1])
        cookie_path.write_text(
            "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t0\tname\tvalue\n",
            encoding="utf-8",
        )
        return object()

    monkeypatch.setattr(authentication_module, "run_command", fake_export)
    adapter = AuthenticatedFixtureAdapter()

    extract_sources(
        ["https://youtu.be/private"],
        Settings(
            cache_root=tmp_path,
            cookies_from_browser="chrome",
            visual_profile="transcript",
            asr_provider="none",
            max_workers=1,
        ),
        workspace_path=tmp_path / "workspace",
        registry=SourceRegistry([adapter]),
    )

    assert exports == 1
    assert len(adapter.cookie_paths) == 2
    assert len(set(adapter.cookie_paths)) == 2
    assert all(not path.exists() for path in adapter.cookie_paths)


def test_partial_inspection_is_persisted_and_disclaimed_in_coverage(tmp_path: Path) -> None:
    workspace, manifest = extract_sources(
        ["fixture://partial"],
        Settings(
            cache_root=tmp_path,
            visual_profile="transcript",
            asr_provider="none",
            max_workers=1,
        ),
        workspace_path=tmp_path / "workspace",
        registry=SourceRegistry([PartialCourseAdapter()]),
    )

    assert manifest.state.value == "partial"
    [report] = workspace.inspection_reports()
    assert report.inaccessible_entries == 1
    coverage = json.loads((workspace.root / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["course_completeness"]["proven"] is False
    assert coverage["course_completeness"]["expected_entries"] == 2
    assert coverage["course_completeness"]["inaccessible_entries"] == 1
    assert coverage["course_completeness"]["inspections"][0]["entries"][1]["reason"] == (
        "private video"
    )


def test_all_inaccessible_inspection_is_persisted_before_extraction_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    with pytest.raises(Exception, match="No processable videos"):
        extract_sources(
            ["fixture://unavailable"],
            Settings(
                cache_root=tmp_path,
                visual_profile="transcript",
                asr_provider="none",
                max_workers=1,
            ),
            workspace_path=root,
            registry=SourceRegistry([UnavailableCourseAdapter()]),
        )

    workspace = Workspace.open(root)
    [report] = workspace.inspection_reports()
    assert report.expected_entries == 1
    assert report.inaccessible_entries == 1
    assert report.entries[0].reason == "deleted video"
    assert workspace.load_manifest().state.value == "failed"
    coverage = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["course_completeness"]["proven"] is False


def test_refresh_tombstones_removed_source_without_deleting_observations(
    tmp_path: Path,
) -> None:
    adapter = MutableCourseAdapter()
    settings = Settings(
        cache_root=tmp_path,
        visual_profile="transcript",
        asr_provider="none",
        max_workers=1,
    )
    workspace, _ = extract_sources(
        ["fixture://mutable"],
        settings,
        workspace_path=tmp_path / "workspace",
        registry=SourceRegistry([adapter]),
    )
    observation = AgentObservation(
        source_id="youtube-one",
        start=0,
        end=1,
        type=ObservationType.CONCEPT,
        claim="Lesson one contains retained evidence.",
        confidence=0.9,
        producer=ObservationProducer(name="test"),
    )
    workspace.upsert_observations([observation])
    adapter.source_ids = ["two"]

    refreshed, _ = extract_sources(
        ["fixture://mutable"],
        settings,
        workspace_path=tmp_path / "workspace",
        registry=SourceRegistry([adapter]),
        refresh=True,
    )

    assert [item.id for item in refreshed.list_sources()] == ["youtube-two"]
    assert [item.source.id for item in refreshed.list_retired_sources()] == ["youtube-one"]
    assert refreshed.observations("youtube-one") == [observation]
    assert len(refreshed.transcripts("youtube-one")) == 2


def test_visual_failure_keeps_last_successful_baseline_and_investigation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        cache_root=tmp_path,
        visual_profile="adaptive",
        asr_provider="none",
        max_workers=1,
    )
    baseline = VisualEvent(
        id="baseline",
        source_id="youtube-fixture",
        timestamp=1,
        path=tmp_path / "baseline.jpg",
        kind=VisualKind.SCENE,
    )
    monkeypatch.setattr(
        pipeline_module,
        "analyze_visuals",
        lambda materialized, selected_settings: ([baseline], []),
    )
    workspace, _ = extract_sources(
        ["fixture://course"],
        settings,
        workspace_path=tmp_path / "workspace",
        registry=SourceRegistry([FixtureAdapter()]),
    )
    dense = VisualEvent(
        id="dense",
        source_id="youtube-fixture",
        timestamp=1.5,
        path=tmp_path / "dense.jpg",
        kind=VisualKind.CODE,
        origin=VisualOrigin.INVESTIGATION,
    )
    workspace.upsert_visuals([dense])
    with workspace.connect() as connection:
        connection.execute(
            "DELETE FROM stages WHERE source_id=? AND stage=?",
            ("youtube-fixture", "visuals"),
        )

    def fail_visuals(
        materialized: MaterializedSource,
        selected_settings: Settings,
    ) -> tuple[list[VisualEvent], list[str]]:
        del materialized, selected_settings
        raise RuntimeError("vision backend failed")

    monkeypatch.setattr(pipeline_module, "analyze_visuals", fail_visuals)
    rerun, _ = extract_sources(
        ["fixture://course"],
        settings,
        workspace_path=tmp_path / "workspace",
        registry=SourceRegistry([FixtureAdapter()]),
    )

    assert {item.id for item in rerun.visuals("youtube-fixture")} == {
        baseline.id,
        dense.id,
    }


def test_missing_asr_fails_before_remote_download(tmp_path: Path) -> None:
    adapter = NoCaptionAdapter()
    settings = Settings(
        cache_root=tmp_path,
        asr_provider="none",
        visual_profile="adaptive",
        max_workers=1,
    )
    with pytest.raises(Exception, match="All sources failed"):
        extract_sources(
            ["fixture://no-captions"],
            settings,
            workspace_path=tmp_path / "workspace",
            registry=SourceRegistry([adapter]),
        )
    assert not adapter.materialized
