from pathlib import Path

import pytest

from video_to_skill.config import Settings
from video_to_skill.models import (
    AgentObservation,
    EvidenceGap,
    EvidenceGapSeverity,
    EvidenceGapType,
    ObservationProducer,
    ObservationType,
    ProcessingStage,
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
    VisualEvent,
    VisualKind,
    VisualOrigin,
    WarningRecord,
)
from video_to_skill.workspace import Workspace


def test_workspace_roundtrip_and_fts(tmp_path: Path) -> None:
    settings = Settings(cache_root=tmp_path)
    workspace = Workspace.create(
        root=tmp_path / "job",
        inputs=["demo"],
        settings=settings,
        job_id="job",
    )
    source = SourceDescriptor(
        id="source",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/demo.mp4",
        title="Demo",
    )
    workspace.upsert_sources([source])
    segment = TranscriptSegment(
        id="transcript",
        source_id=source.id,
        start=1,
        end=2,
        text="Configure a durable database",
        origin=TranscriptOrigin.MANUAL_CAPTION,
    )
    workspace.replace_transcripts(source.id, [segment])
    assert workspace.transcripts(source.id)[0] == segment
    assert workspace.transcripts(source.id, search="durable")[0] == segment

    workspace.start_stage(source.id, ProcessingStage.TRANSCRIBE, "key")
    assert not workspace.stage_complete(source.id, ProcessingStage.TRANSCRIBE, "key")
    workspace.complete_stage(source.id, ProcessingStage.TRANSCRIBE, "key")
    assert workspace.stage_complete(source.id, ProcessingStage.TRANSCRIBE, "key")
    assert not workspace.stage_complete(source.id, ProcessingStage.TRANSCRIBE, "other")

    workspace.add_warning(
        WarningRecord(
            code="retryable",
            message="old failure",
            source_id=source.id,
            stage=ProcessingStage.TRANSCRIBE,
        )
    )
    workspace.start_stage(source.id, ProcessingStage.TRANSCRIBE, "new")
    assert workspace.list_warnings() == []


def test_workspace_rejects_changed_configuration(tmp_path: Path) -> None:
    root = tmp_path / "job"
    Workspace.create(
        root=root,
        inputs=["demo"],
        settings=Settings(scene_threshold=0.3),
    )
    try:
        Workspace.create(
            root=root,
            inputs=["demo"],
            settings=Settings(scene_threshold=0.7),
        )
    except Exception as exc:
        assert "configuration differs" in str(exc)
    else:
        raise AssertionError("changed configuration should be rejected")


def test_invalid_fts_query_is_actionable(tmp_path: Path) -> None:
    settings = Settings(cache_root=tmp_path)
    workspace = Workspace.create(
        root=tmp_path / "job",
        inputs=["demo"],
        settings=settings,
    )
    source = SourceDescriptor(
        id="source",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/demo.mp4",
        title="Demo",
    )
    workspace.upsert_sources([source])
    try:
        workspace.transcripts(source.id, search='"unterminated')
    except Exception as exc:
        assert "Invalid full-text search query" in str(exc)
    else:
        raise AssertionError("invalid FTS syntax should be rejected")


def test_source_prune_tombstones_and_preserves_agent_evidence(tmp_path: Path) -> None:
    workspace = Workspace.create(
        root=tmp_path / "job",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    removed = SourceDescriptor(
        id="removed",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/removed.mp4",
        title="Removed",
    )
    retained = SourceDescriptor(
        id="retained",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/retained.mp4",
        title="Retained",
    )
    workspace.upsert_sources([removed, retained])
    observation = AgentObservation(
        source_id=removed.id,
        start=0,
        end=1,
        type=ObservationType.CONCEPT,
        claim="The source demonstrates a durable invariant.",
        confidence=0.9,
        producer=ObservationProducer(name="test"),
    )
    gap = EvidenceGap(
        source_id=removed.id,
        gap_type=EvidenceGapType.UNOBSERVED_CLAIM,
        severity=EvidenceGapSeverity.WARNING,
        message="The claim needs another observation.",
        suggested_next_action="Inspect the retained evidence.",
        start=0,
        end=1,
    )
    workspace.upsert_observations([observation])
    workspace.upsert_gaps([gap])

    workspace.upsert_sources([retained], prune=True)

    assert [item.id for item in workspace.list_sources()] == [retained.id]
    assert {item.id for item in workspace.list_sources(include_inactive=True)} == {
        removed.id,
        retained.id,
    }
    [tombstone] = workspace.list_retired_sources()
    assert tombstone.source.id == removed.id
    assert "latest successful source inspection" in tombstone.reason
    assert workspace.observations(removed.id) == [observation]
    assert workspace.gaps(removed.id) == [gap]


def test_baseline_visual_replacement_preserves_investigation_and_observations(
    tmp_path: Path,
) -> None:
    workspace = Workspace.create(
        root=tmp_path / "job",
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
    first_baseline = VisualEvent(
        id="baseline-old",
        source_id=source.id,
        timestamp=1,
        path=tmp_path / "baseline-old.jpg",
        kind=VisualKind.SCENE,
    )
    dense = VisualEvent(
        id="dense-investigation",
        source_id=source.id,
        timestamp=1.5,
        path=tmp_path / "dense.jpg",
        kind=VisualKind.CODE,
        origin=VisualOrigin.INVESTIGATION,
    )
    workspace.replace_visuals(source.id, [first_baseline])
    workspace.upsert_visuals([dense])
    observation = AgentObservation(
        source_id=source.id,
        start=1,
        end=2,
        type=ObservationType.CODE,
        claim="The dense frame shows the implementation.",
        frame_ids=[dense.id],
        confidence=0.95,
        producer=ObservationProducer(name="test"),
    )
    workspace.upsert_observations([observation])
    replacement = VisualEvent(
        id="baseline-new",
        source_id=source.id,
        timestamp=2,
        path=tmp_path / "baseline-new.jpg",
        kind=VisualKind.PERIODIC,
    )

    workspace.replace_visuals(source.id, [replacement])

    assert {item.id for item in workspace.visuals(source.id)} == {
        replacement.id,
        dense.id,
    }
    assert workspace.observations(source.id) == [observation]

    workspace.replace_visuals(source.id, [])
    assert workspace.visuals(source.id) == [dense]
    assert workspace.observations(source.id) == [observation]


def test_visual_replacement_checks_source_and_origin_ownership(tmp_path: Path) -> None:
    workspace = Workspace.create(
        root=tmp_path / "job",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    first = SourceDescriptor(
        id="first",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/first.mp4",
        title="First",
    )
    second = SourceDescriptor(
        id="second",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/second.mp4",
        title="Second",
    )
    workspace.upsert_sources([first, second])
    dense = VisualEvent(
        id="shared-id",
        source_id=first.id,
        timestamp=1,
        path=tmp_path / "dense.jpg",
        origin=VisualOrigin.INVESTIGATION,
    )
    workspace.upsert_visuals([dense])

    with pytest.raises(Exception, match="belong to the source"):
        workspace.replace_visuals(
            first.id,
            [
                VisualEvent(
                    id="wrong-source",
                    source_id=second.id,
                    timestamp=2,
                    path=tmp_path / "wrong.jpg",
                )
            ],
        )
    with pytest.raises(Exception, match="already belongs to origin"):
        workspace.replace_visuals(
            first.id,
            [
                VisualEvent(
                    id=dense.id,
                    source_id=first.id,
                    timestamp=2,
                    path=tmp_path / "baseline.jpg",
                )
            ],
        )
    assert workspace.visuals(first.id) == [dense]


def test_v2_workspace_migrates_source_tombstones_and_visual_origins(tmp_path: Path) -> None:
    workspace = Workspace.create(
        root=tmp_path / "job",
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
    with workspace.connect() as connection:
        connection.execute("DROP INDEX visual_source_origin_time")
        connection.execute("ALTER TABLE visual_events DROP COLUMN origin")
        connection.execute("ALTER TABLE sources DROP COLUMN removal_reason")
        connection.execute("ALTER TABLE sources DROP COLUMN removed_at")
        connection.execute("ALTER TABLE sources DROP COLUMN active")
        connection.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")

    migrated = Workspace.open(workspace.root)

    with migrated.connect() as connection:
        source_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sources)")}
        visual_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(visual_events)")
        }
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()["value"]
    assert {"active", "removed_at", "removal_reason"} <= source_columns
    assert "origin" in visual_columns
    assert version == "4"
    assert migrated.get_source(source.id) == source
