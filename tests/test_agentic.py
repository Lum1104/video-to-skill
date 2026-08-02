from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event, Thread

import pytest

from video_to_skill.agentic import (
    MAX_ANNOTATION_PAYLOAD_BYTES,
    MAX_ANNOTATIONS_PER_BATCH,
    analyze_evidence_gaps,
    assemble_agent_context,
    detect_evidence_gaps,
    ingest_annotations,
)
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.models import (
    AgentObservation,
    EvidenceGap,
    EvidenceGapType,
    ObservationType,
    SemanticSegment,
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
    VisualEvent,
    VisualKind,
)
from video_to_skill.workspace import SCHEMA_VERSION, Workspace


def _workspace(tmp_path: Path) -> Workspace:
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
        duration=240,
    )
    workspace.upsert_sources([source])
    transcripts = [
        TranscriptSegment(
            id="t-intro",
            source_id=source.id,
            start=0,
            end=25,
            text="This lesson introduces the durable workflow.",
            origin=TranscriptOrigin.MANUAL_CAPTION,
        ),
        TranscriptSegment(
            id="t-click",
            source_id=source.id,
            start=30,
            end=110,
            text="Click Open, configure the database, and run the command.",
            origin=TranscriptOrigin.MANUAL_CAPTION,
        ),
        TranscriptSegment(
            id="t-select",
            source_id=source.id,
            start=120,
            end=205,
            text="Select the result and press Apply to finish the procedure.",
            origin=TranscriptOrigin.MANUAL_CAPTION,
        ),
    ]
    visuals = [
        VisualEvent(
            id="v-slide",
            source_id=source.id,
            timestamp=10,
            path=tmp_path / "slide.jpg",
            kind=VisualKind.SLIDE,
        ),
        VisualEvent(
            id="v-code",
            source_id=source.id,
            timestamp=45,
            path=tmp_path / "code.jpg",
            kind=VisualKind.CODE,
        ),
    ]
    segments = [
        SemanticSegment(
            id="section-one",
            source_id=source.id,
            ordinal=1,
            title="Configure",
            start=0,
            end=60,
            transcript_ids=["t-intro", "t-click"],
            visual_event_ids=["v-slide", "v-code"],
        ),
        SemanticSegment(
            id="section-two",
            source_id=source.id,
            ordinal=2,
            title="Apply",
            start=60,
            end=210,
            transcript_ids=["t-click", "t-select"],
            visual_event_ids=[],
        ),
    ]
    workspace.replace_transcripts(source.id, transcripts)
    workspace.replace_visuals(source.id, visuals)
    workspace.replace_semantic_segments(source.id, segments)
    return workspace


def _valid_annotation() -> dict[str, object]:
    return {
        "source_id": "source",
        "start": 40,
        "end": 50,
        "type": "code",
        "claim": "The editor shows a database configuration command.",
        "frame_ids": ["v-code"],
        "transcript_ids": ["t-click"],
        "confidence": 0.95,
        "status": "observed",
        "uncertainty": None,
        "producer": {"name": "test-agent", "model": "fixture-model"},
    }


def test_v1_workspace_is_migrated_without_losing_sources(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("DROP TABLE agent_observations")
        connection.execute("DROP TABLE evidence_gaps")
        connection.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
        connection.commit()

    reopened = Workspace.open(workspace.root)

    assert reopened.get_source("source").title == "Demo"
    with reopened.connect() as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name IN ('agent_observations', 'evidence_gaps')
                """
            )
        }
    assert version is not None
    assert int(version["value"]) == SCHEMA_VERSION
    assert tables == {"agent_observations", "evidence_gaps"}


def test_context_requires_one_finite_bound_and_includes_observations(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    [observation] = ingest_annotations(workspace, _valid_annotation())

    section_context = assemble_agent_context(
        workspace,
        source_id="source",
        section=1,
    )
    assert section_context.window.start == 0
    assert section_context.window.end == 60
    assert section_context.observations == [observation]
    assert [item.id for item in section_context.segments] == ["section-one"]

    timestamp_context = assemble_agent_context(
        workspace,
        source_id="source",
        at=100,
        window=20,
    )
    assert timestamp_context.window.start == 80
    assert timestamp_context.window.end == 120

    with pytest.raises(ProcessingError, match="exactly one bound"):
        assemble_agent_context(workspace, source_id="source")
    with pytest.raises(ProcessingError, match="exactly one bound"):
        assemble_agent_context(
            workspace,
            source_id="source",
            section=1,
            at=40,
            window=10,
        )
    with pytest.raises(ProcessingError, match="maximum"):
        assemble_agent_context(
            workspace,
            source_id="source",
            start=0,
            end=240,
            max_window_seconds=60,
        )
    with pytest.raises(ProcessingError, match="maximum"):
        assemble_agent_context(
            workspace,
            source_id="source",
            at=120,
            window=31,
            max_window_seconds=60,
        )


def test_annotation_ingestion_is_strict_grounded_and_atomic(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    [observation] = ingest_annotations(
        workspace,
        {"observations": [_valid_annotation()]},
    )
    assert observation.id.startswith("obs_")
    assert observation.type == ObservationType.CODE
    assert workspace.get_observation(observation.id) == observation

    changed = {
        **_valid_annotation(),
        "id": observation.id,
        "claim": "The editor visibly contains a database command.",
    }
    [updated] = ingest_annotations(workspace, changed)
    assert updated.id == observation.id
    assert workspace.get_observation(observation.id).claim == changed["claim"]

    invalid = {
        **_valid_annotation(),
        "claim": "This object must make the whole batch fail.",
        "unexpected": True,
    }
    before = workspace.observations("source")
    with pytest.raises(ProcessingError, match="Invalid agent observation"):
        ingest_annotations(workspace, [_valid_annotation(), invalid])
    assert workspace.observations("source") == before

    outside_duration = {**_valid_annotation(), "start": 241, "end": 242}
    with pytest.raises(ProcessingError, match="beyond source duration"):
        ingest_annotations(workspace, outside_duration)

    unknown_frame = {**_valid_annotation(), "frame_ids": ["not-a-frame"]}
    with pytest.raises(ProcessingError, match="outside source"):
        ingest_annotations(workspace, unknown_frame)

    distant_frame = {
        **_valid_annotation(),
        "start": 100,
        "end": 105,
        "transcript_ids": ["t-click"],
    }
    with pytest.raises(ProcessingError, match="not within"):
        ingest_annotations(workspace, distant_frame)


def test_annotation_payload_size_and_count_are_bounded(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert MAX_ANNOTATION_PAYLOAD_BYTES > 0
    assert MAX_ANNOTATIONS_PER_BATCH > 0

    with pytest.raises(ProcessingError, match="byte limit"):
        ingest_annotations(
            workspace,
            " " * 33,
            max_payload_bytes=32,
        )

    oversized_path = tmp_path / "oversized.json"
    oversized_path.write_text(" " * 33, encoding="utf-8")
    with pytest.raises(ProcessingError, match="byte limit"):
        ingest_annotations(
            workspace,
            oversized_path,
            max_payload_bytes=32,
        )

    with pytest.raises(ProcessingError, match="maximum is 1"):
        ingest_annotations(
            workspace,
            [_valid_annotation(), _valid_annotation()],
            max_observations=1,
        )

    oversized_mapping = {
        **_valid_annotation(),
        "claim": "x" * 1_000,
    }
    with pytest.raises(ProcessingError, match="byte limit"):
        ingest_annotations(
            workspace,
            oversized_mapping,
            max_payload_bytes=128,
        )

    with pytest.raises(ProcessingError, match="byte limit"):
        ingest_annotations(
            workspace,
            [oversized_mapping],
            max_payload_bytes=128,
        )
    assert workspace.observations("source") == []


def test_annotation_rejects_cross_source_references(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    other = SourceDescriptor(
        id="other",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/other.mp4",
        title="Other",
        duration=60,
    )
    workspace.upsert_sources([workspace.get_source("source"), other])
    workspace.replace_visuals(
        other.id,
        [
            VisualEvent(
                id="other-frame",
                source_id=other.id,
                timestamp=45,
                path=tmp_path / "other.jpg",
                kind=VisualKind.CODE,
            )
        ],
    )
    annotation = {**_valid_annotation(), "frame_ids": ["other-frame"]}
    with pytest.raises(ProcessingError, match="outside source"):
        ingest_annotations(workspace, annotation)


def test_annotation_validation_queries_only_referenced_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)

    def reject_bulk_load(*args: object, **kwargs: object) -> None:
        raise AssertionError("annotation validation must not bulk-load evidence")

    monkeypatch.setattr(workspace, "list_sources", reject_bulk_load)
    monkeypatch.setattr(workspace, "transcripts", reject_bulk_load)
    monkeypatch.setattr(workspace, "visuals", reject_bulk_load)

    [observation] = ingest_annotations(workspace, _valid_annotation())

    assert workspace.get_observation(observation.id) == observation


def test_annotation_reference_validation_and_upsert_share_write_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    validation_complete = Event()
    allow_upsert = Event()
    failures: list[BaseException] = []
    original_validate = workspace._validate_observation_references

    def pause_after_validation(
        connection: sqlite3.Connection,
        observations: list[AgentObservation],
        neighborhood_seconds: float,
    ) -> None:
        original_validate(connection, observations, neighborhood_seconds)
        validation_complete.set()
        if not allow_upsert.wait(timeout=5):
            raise AssertionError("timed out waiting to finish annotation upsert")

    monkeypatch.setattr(
        workspace,
        "_validate_observation_references",
        pause_after_validation,
    )

    def ingest_in_thread() -> None:
        try:
            ingest_annotations(workspace, _valid_annotation())
        except BaseException as exc:
            failures.append(exc)

    worker = Thread(target=ingest_in_thread)
    worker.start()
    assert validation_complete.wait(timeout=5)
    try:
        with (
            closing(sqlite3.connect(workspace.database_path, timeout=0.05)) as competing,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            competing.execute("DELETE FROM visual_events WHERE id='v-code'")
            competing.commit()
    finally:
        allow_upsert.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    assert {item.id for item in workspace.visuals("source")} >= {"v-code"}
    assert len(workspace.observations("source")) == 1


def test_upserted_targeted_frame_clears_section_visual_gap(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = detect_evidence_gaps(workspace)
    assert any(
        gap.gap_type == EvidenceGapType.NO_VISUAL_EVIDENCE
        and gap.semantic_segment_id == "section-two"
        for gap in before
    )

    targeted = VisualEvent(
        id="v-targeted",
        source_id="source",
        timestamp=100,
        path=tmp_path / "targeted.jpg",
        kind=VisualKind.UI,
        description="The result panel is open.",
    )
    workspace.upsert_visuals([targeted])

    assert {item.id for item in workspace.visuals("source")} == {
        "v-slide",
        "v-code",
        "v-targeted",
    }
    after = detect_evidence_gaps(workspace)
    assert not any(
        gap.gap_type == EvidenceGapType.NO_VISUAL_EVIDENCE
        and gap.semantic_segment_id == "section-two"
        for gap in after
    )

    changed = targeted.model_copy(update={"description": "The result panel shows success."})
    workspace.upsert_visuals([changed])
    assert (
        next(item for item in workspace.visuals("source") if item.id == targeted.id).description
        == "The result panel shows success."
    )

    other = SourceDescriptor(
        id="other",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/other.mp4",
        title="Other",
        duration=60,
    )
    workspace.upsert_sources([workspace.get_source("source"), other])
    moved = changed.model_copy(update={"source_id": other.id})
    with pytest.raises(ProcessingError, match="already belongs"):
        workspace.upsert_visuals([moved])
    assert workspace.visuals(other.id) == []


def test_context_limits_visuals_in_sql_before_deserialization(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.upsert_visuals(
        [
            VisualEvent(
                id="v-third",
                source_id="source",
                timestamp=50,
                path=tmp_path / "third.jpg",
                kind=VisualKind.UI,
            )
        ]
    )
    with workspace.connect() as connection:
        connection.execute(
            """
            INSERT INTO visual_events(id, source_id, timestamp, origin, data_json)
            VALUES('v-invalid', 'source', 55, 'baseline', 'not-json')
            """
        )

    context = assemble_agent_context(
        workspace,
        source_id="source",
        section=1,
        max_items_per_kind=2,
    )

    assert [item.id for item in context.visuals] == ["v-slide", "v-code"]
    assert context.truncated


def test_gap_detection_is_deterministic_and_covers_required_classes(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.upsert_observations(
        [
            AgentObservation(
                source_id="source",
                start=130,
                end=140,
                type=ObservationType.CONCEPT,
                claim="A result is discussed without direct grounding.",
                frame_ids=[],
                transcript_ids=[],
                confidence=0.5,
                status="inferred",
                uncertainty="No source references were attached.",
                producer={"name": "test-agent"},
            )
        ]
    )

    first = detect_evidence_gaps(workspace)
    second = detect_evidence_gaps(workspace)

    assert first == second
    gap_types = {gap.gap_type for gap in first}
    assert {
        EvidenceGapType.NO_VISUAL_EVIDENCE,
        EvidenceGapType.UNOBSERVED_CLAIM,
        EvidenceGapType.UNGROUNDED_OBSERVATION,
        EvidenceGapType.WEAK_VISUAL_COVERAGE,
    } <= gap_types
    assert len({gap.id for gap in first}) == len(first)
    assert all(gap.suggested_next_action for gap in first)


def test_gap_identity_fingerprints_missing_type_and_relevant_evidence(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    common = {
        "source_id": "source",
        "semantic_segment_id": "section-one",
        "gap_type": EvidenceGapType.UNOBSERVED_CLAIM,
        "severity": "warning",
        "message": "A visible claim has no matching observation.",
        "related_transcript_ids": ["t-click"],
        "related_visual_ids": ["v-code"],
        "suggested_next_action": "Inspect and annotate the visible claim.",
        "start": 0,
        "end": 60,
    }
    code_gap = EvidenceGap(
        **common,
        missing_observation_types=[ObservationType.CODE],
    )
    ui_gap = EvidenceGap(
        **common,
        missing_observation_types=[ObservationType.UI],
    )
    other_evidence_gap = EvidenceGap(
        **{
            **common,
            "related_visual_ids": ["v-slide"],
            "missing_observation_types": [ObservationType.CODE],
        }
    )

    assert len({code_gap.id, ui_gap.id, other_evidence_gap.id}) == 3
    workspace.upsert_gaps([code_gap])
    workspace.resolve_gap(code_gap.id, resolution="The code claim was reviewed.")
    workspace.replace_gaps("source", [ui_gap], preserve_resolution=True)

    [persisted] = workspace.gaps("source", limit=None)
    assert persisted.id == ui_gap.id
    assert not persisted.resolved
    assert persisted.resolution is None


def test_gap_detection_reports_each_missing_reference_in_partial_lists(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    observation = AgentObservation(
        id="obs-partial-grounding",
        source_id="source",
        start=40,
        end=50,
        type=ObservationType.CODE,
        claim="The code observation contains partially stale references.",
        frame_ids=["v-code", "v-missing"],
        transcript_ids=["t-click", "t-missing"],
        confidence=0.8,
        producer={"name": "test-agent"},
    )
    workspace.upsert_observations([observation])

    gap = next(
        item
        for item in detect_evidence_gaps(workspace)
        if item.gap_type == EvidenceGapType.UNGROUNDED_OBSERVATION
        and item.related_observation_ids == [observation.id]
    )

    assert gap.missing_visual_ids == ["v-missing"]
    assert gap.missing_transcript_ids == ["t-missing"]
    assert gap.severity == "error"


def test_clustered_visuals_do_not_clear_long_procedural_coverage_gap(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.upsert_visuals(
        [
            VisualEvent(
                id=f"v-cluster-{index}",
                source_id="source",
                timestamp=61 + index,
                path=tmp_path / f"cluster-{index}.jpg",
                kind=VisualKind.UI,
            )
            for index in range(3)
        ]
    )

    gaps = detect_evidence_gaps(workspace)

    assert any(
        gap.gap_type == EvidenceGapType.WEAK_VISUAL_COVERAGE
        and gap.semantic_segment_id == "section-two"
        and "147s interval" in gap.message
        for gap in gaps
    )


def test_gap_reanalysis_returns_every_generated_gap_beyond_default_page(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.upsert_observations(
        [
            AgentObservation(
                id=f"obs-bulk-{index:04d}",
                source_id="source",
                start=130,
                end=131,
                type=ObservationType.CONCEPT,
                claim=f"Ungrounded bulk observation {index}.",
                confidence=0.5,
                status="inferred",
                uncertainty="This fixture intentionally has no references.",
                producer={"name": "test-agent"},
            )
            for index in range(1_005)
        ]
    )
    detected = detect_evidence_gaps(workspace)

    persisted = analyze_evidence_gaps(workspace)

    assert len(detected) > 1_000
    assert [gap.id for gap in persisted] == [gap.id for gap in detected]
    assert [gap.id for gap in workspace.gaps("source", limit=None)] == [gap.id for gap in detected]


def test_gap_resolution_and_safe_updates_survive_reanalysis(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    gaps = analyze_evidence_gaps(workspace)
    target = next(gap for gap in gaps if gap.gap_type == EvidenceGapType.NO_VISUAL_EVIDENCE)

    resolved = workspace.resolve_gap(target.id, resolution="Reviewed source manually.")
    assert resolved.resolved
    assert resolved.resolved_at is not None

    refreshed = analyze_evidence_gaps(workspace)
    preserved = next(gap for gap in refreshed if gap.id == target.id)
    assert preserved.resolved
    assert preserved.resolution == "Reviewed source manually."

    updated = EvidenceGap.model_validate(
        {
            **preserved.model_dump(),
            "message": "Updated actionable explanation.",
        }
    )
    workspace.upsert_gaps([updated])
    assert workspace.get_gap(target.id).message == "Updated actionable explanation."

    reopened = workspace.resolve_gap(target.id, resolved=False)
    assert not reopened.resolved
    assert reopened.resolved_at is None
    assert reopened.resolution is None
    assert workspace.delete_gap(target.id)
    assert target.id not in {gap.id for gap in workspace.gaps()}
