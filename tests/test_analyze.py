from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_to_skill.analyze import (
    plan_analyze_integration_task,
    plan_analyze_tasks,
    submit_analyze_result,
)
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import SemanticUnit
from video_to_skill.models import (
    AgentObservation,
    EvidenceGap,
    EvidenceGapSeverity,
    EvidenceGapType,
    ObservationProducer,
    ObservationType,
    SemanticSegment,
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
    VisualEvent,
    VisualOrigin,
)
from video_to_skill.orchestration import (
    AnalyzeResult,
    CapabilityEvidence,
    SemanticCoverage,
)
from video_to_skill.utils import atomic_write_json, stable_hash
from video_to_skill.work import WorkState
from video_to_skill.workspace import Workspace


def _workspace(tmp_path: Path, *, sections: int = 1) -> Workspace:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    source = SourceDescriptor(
        id="source",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/demo.mp4",
        title="Agent architecture interview",
        duration=float(sections * 120),
    )
    workspace.upsert_sources([source])
    transcripts = [
        TranscriptSegment(
            id=f"transcript-{ordinal}",
            source_id=source.id,
            start=float((ordinal - 1) * 120),
            end=float((ordinal - 1) * 120 + 60),
            text=f"Principle {ordinal} needs evidence, a qualification, and an example.",
            origin=TranscriptOrigin.MANUAL_CAPTION,
        )
        for ordinal in range(1, sections + 1)
    ]
    semantic_sections = [
        SemanticSegment(
            id=f"section-{ordinal}",
            source_id=source.id,
            ordinal=ordinal,
            title=f"Principle {ordinal}",
            start=float((ordinal - 1) * 120),
            end=float(ordinal * 120),
            transcript_ids=[f"transcript-{ordinal}"],
        )
        for ordinal in range(1, sections + 1)
    ]
    workspace.replace_transcripts(source.id, transcripts)
    workspace.replace_semantic_segments(source.id, semantic_sections)
    return workspace


def _populate_dense_evidence(
    workspace: Workspace,
    *,
    sections: int,
    per_category: int = 15,
) -> None:
    transcripts: list[TranscriptSegment] = []
    visuals: list[VisualEvent] = []
    observations: list[AgentObservation] = []
    gaps: list[EvidenceGap] = []
    for ordinal in range(1, sections + 1):
        base = float((ordinal - 1) * 120)
        for index in range(per_category):
            timestamp = base + index + 1
            transcripts.append(
                TranscriptSegment(
                    id=f"transcript-{ordinal}-{index}",
                    source_id="source",
                    start=timestamp,
                    end=timestamp + 0.5,
                    text=f"Evidence {ordinal}/{index}",
                    origin=TranscriptOrigin.MANUAL_CAPTION,
                )
            )
            visuals.append(
                VisualEvent(
                    id=f"visual-{ordinal}-{index}",
                    source_id="source",
                    timestamp=timestamp,
                    path=workspace.root / "frames" / f"{ordinal}-{index}.jpg",
                )
            )
            observations.append(
                AgentObservation(
                    source_id="source",
                    start=timestamp,
                    end=timestamp + 0.5,
                    type=ObservationType.CONCEPT,
                    claim=f"Observation {ordinal}/{index}",
                    confidence=0.9,
                    producer=ObservationProducer(name="dense-test"),
                )
            )
            gaps.append(
                EvidenceGap(
                    source_id="source",
                    gap_type=EvidenceGapType.UNOBSERVED_CLAIM,
                    severity=EvidenceGapSeverity.WARNING,
                    message=f"Gap {ordinal}/{index}",
                    suggested_next_action="Inspect the bounded evidence window.",
                    start=timestamp,
                    end=timestamp + 0.5,
                )
            )
    workspace.replace_transcripts("source", transcripts)
    workspace.replace_visuals("source", visuals)
    workspace.upsert_observations(observations)
    workspace.upsert_gaps(gaps)


def _set_analyze_packet_limit(workspace: Workspace, limit: int) -> None:
    manifest = workspace.load_manifest()
    assert manifest.analysis_depth is not None
    budget = manifest.analysis_depth.budget.model_copy(update={"analyze_packet_item_limit": limit})
    manifest.analysis_depth = manifest.analysis_depth.model_copy(
        update={
            "budget": budget,
            "budget_digest": stable_hash(budget.model_dump(mode="json"), length=64),
        }
    )
    workspace.save_manifest(manifest)


def _result(
    *,
    task_id: str,
    lease_token: str,
    snapshot_digest: str,
    evidence_id: str = "transcript-1",
    integrated: bool = True,
    unit_id: str = "unit-principle",
    start: float = 0,
) -> AnalyzeResult:
    unit = SemanticUnit(
        id=unit_id,
        source_id="source",
        start=start,
        end=start + 60,
        kind="claim",
        summary="Durable systems preserve grounded evidence.",
        materiality="core",
        disposition="included",
        inferred=False,
        confidence="high",
        modalities=["speech"],
        evidence_ids=[evidence_id],
    )
    return AnalyzeResult(
        task_id=task_id,
        lease_token=lease_token,
        snapshot_digest=snapshot_digest,
        producer=ObservationProducer(name="analysis-worker", run_id="worker-1"),
        integrated=integrated,
        semantic_units=[unit],
        capability_evidence=[
            CapabilityEvidence(
                mode=mode,
                ceiling="medium",
                semantic_unit_ids=[unit.id],
                rationale="The source explains a transferable principle.",
            )
            for mode in ("learn", "practice", "apply", "reference")
        ],
        coverage=SemanticCoverage(
            source_ids=["source"],
            core_units=1,
            supporting_units=0,
            contextual_units=0,
            incidental_units=0,
            included_units=1,
            merged_units=0,
            context_only_units=0,
            omitted_units=0,
            material_units_accounted_for=True,
        ),
    )


def test_short_course_analyze_task_is_speech_first_and_persists_result(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    run = workspace.create_analysis_run()
    [task] = plan_analyze_tasks(workspace, run)
    lease = workspace.lease_work_item(task.id, owner="codex")
    result = _result(
        task_id=task.id,
        lease_token=lease.token,
        snapshot_digest=task.snapshot_digest,
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    accepted = submit_analyze_result(workspace, task.id, result_path)

    assert accepted.state == WorkState.COMPLETE
    assert workspace.canonical_record("semantic-map") is not None
    packet = (workspace.root / task.packet_path).read_text(encoding="utf-8")
    assert '"route": "speech-first"' in packet
    assert '"transcript-1"' in packet


def test_analyze_submission_rejects_evidence_outside_packet(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    run = workspace.create_analysis_run()
    [task] = plan_analyze_tasks(workspace, run)
    lease = workspace.lease_work_item(task.id, owner="codex")
    result = _result(
        task_id=task.id,
        lease_token=lease.token,
        snapshot_digest=task.snapshot_digest,
        evidence_id="invented-evidence",
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    with pytest.raises(ProcessingError, match="outside its Analyze packet"):
        submit_analyze_result(workspace, task.id, result_path)

    assert workspace.get_work_item(task.id).state == WorkState.LEASED


def test_analyze_submission_rejects_tampered_task_packet(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    run = workspace.create_analysis_run()
    [task] = plan_analyze_tasks(workspace, run)
    lease = workspace.lease_work_item(task.id, owner="codex")
    packet_path = workspace.root / task.packet_path
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    packet["payload"]["allowed_evidence_ids"].append("invented-evidence")
    atomic_write_json(packet_path, packet)
    result = _result(
        task_id=task.id,
        lease_token=lease.token,
        snapshot_digest=task.snapshot_digest,
        evidence_id="invented-evidence",
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    with pytest.raises(ProcessingError, match="packet failed its digest check"):
        submit_analyze_result(workspace, task.id, result_path)


def test_analyze_submission_accepts_bounded_investigation_frames(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    run = workspace.create_analysis_run()
    [task] = plan_analyze_tasks(workspace, run)
    lease = workspace.lease_work_item(task.id, owner="codex")
    frame_path = workspace.source_directory("source") / "investigation-frames" / "frame.jpg"
    frame_path.parent.mkdir()
    frame_path.write_bytes(b"frame")
    frame = VisualEvent(
        id="investigation-frame",
        source_id="source",
        timestamp=30,
        path=frame_path,
        origin=VisualOrigin.INVESTIGATION,
    )
    workspace.upsert_visuals([frame])
    result = _result(
        task_id=task.id,
        lease_token=lease.token,
        snapshot_digest=task.snapshot_digest,
        evidence_id=frame.id,
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    accepted = submit_analyze_result(workspace, task.id, result_path)

    assert accepted.state == WorkState.COMPLETE


def test_analyze_submission_rejects_investigation_frames_outside_task_sections(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, sections=25)
    run = workspace.create_analysis_run()
    [task, *_remaining] = plan_analyze_tasks(workspace, run)
    lease = workspace.lease_work_item(task.id, owner="codex")
    frame_path = workspace.source_directory("source") / "investigation-frames" / "frame.jpg"
    frame_path.parent.mkdir()
    frame_path.write_bytes(b"frame")
    frame = VisualEvent(
        id="out-of-scope-investigation-frame",
        source_id="source",
        timestamp=1_000,
        path=frame_path,
        origin=VisualOrigin.INVESTIGATION,
    )
    workspace.upsert_visuals([frame])
    result = _result(
        task_id=task.id,
        lease_token=lease.token,
        snapshot_digest=task.snapshot_digest,
        evidence_id=frame.id,
        integrated=False,
        start=960,
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    with pytest.raises(ProcessingError, match="outside its Analyze packet"):
        submit_analyze_result(workspace, task.id, result_path)


def test_long_course_analyze_tasks_are_bounded_section_groups(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, sections=25)
    run = workspace.create_analysis_run()

    tasks = plan_analyze_tasks(workspace, run)

    assert len(tasks) == 4
    assert all(task.scope["kind"] == "section-group" for task in tasks)
    assert max(len(task.scope["source_sections"]["source"]) for task in tasks) <= 8


def test_integrated_analyze_packet_enforces_one_fair_task_wide_evidence_budget(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, sections=2)
    workspace.create_analysis_run()
    _populate_dense_evidence(workspace, sections=2)
    _set_analyze_packet_limit(workspace, 100)
    run = workspace.create_analysis_run()

    [task] = plan_analyze_tasks(workspace, run)
    payload = json.loads((workspace.root / task.packet_path).read_text(encoding="utf-8"))["payload"]
    budget = payload["evidence_budget"]

    assert budget == {
        "profile_limit": 100,
        "absolute_safety_limit": 3000,
        "effective_limit": 100,
        "allocation": "deterministic-section-category-round-robin",
        "available": 120,
        "included": 100,
        "truncated": 20,
        "complete": False,
    }
    included_from_sections = 0
    section_included: list[int] = []
    for section in payload["sections"]:
        coverage = section["evidence_coverage"]
        assert coverage["available"] == 60
        assert 48 <= coverage["included"] <= 52
        assert coverage["truncated"] == 60 - coverage["included"]
        assert coverage["complete"] is False
        assert all(
            category["available"] == 15 and category["included"] > 0 and category["truncated"] >= 0
            for category in coverage["categories"].values()
        )
        included_from_sections += sum(
            len(section[name]) for name in ("transcripts", "visuals", "observations", "gaps")
        )
        section_included.append(coverage["included"])
    assert included_from_sections == budget["included"]
    assert max(section_included) - min(section_included) <= 4


def test_sharded_analyze_packets_each_enforce_the_task_wide_cap(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, sections=25)
    workspace.create_analysis_run()
    _populate_dense_evidence(workspace, sections=25)
    _set_analyze_packet_limit(workspace, 100)
    run = workspace.create_analysis_run()

    tasks = plan_analyze_tasks(workspace, run)

    assert len(tasks) == 4
    for task in tasks:
        payload = json.loads((workspace.root / task.packet_path).read_text(encoding="utf-8"))[
            "payload"
        ]
        budget = payload["evidence_budget"]
        included = sum(
            len(section[name])
            for section in payload["sections"]
            for name in ("transcripts", "visuals", "observations", "gaps")
        )
        assert included == budget["included"]
        assert included <= 100
        assert budget["effective_limit"] == 100
        assert budget["available"] == 60 * len(payload["sections"])
        assert budget["truncated"] == budget["available"] - included
        assert all(
            category["included"] > 0
            for section in payload["sections"]
            for category in section["evidence_coverage"]["categories"].values()
        )


def test_deep_contract_increases_analyze_fanout_and_binds_task_packet(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, sections=25)
    run = workspace.create_analysis_run(settings=Settings(analysis_depth="deep"))

    tasks = plan_analyze_tasks(workspace, run)

    assert len(tasks) == 5
    assert max(len(task.scope["source_sections"]["source"]) for task in tasks) <= 6
    packet = json.loads((workspace.root / tasks[0].packet_path).read_text(encoding="utf-8"))[
        "payload"
    ]
    assert packet["analysis_depth"]["requested"] == "deep"
    assert packet["analysis_depth"]["effective"] == "deep"
    assert packet["analysis_depth"]["budget"]["analyze_packet_item_limit"] == 2400
    assert packet["investigation_policy"]["max_window_seconds"] == 180
    assert packet["investigation_policy"]["max_frames_per_window"] == 360


def test_analyze_integration_reuses_only_shard_evidence(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, sections=25)
    run = workspace.create_analysis_run()
    shard_tasks = plan_analyze_tasks(workspace, run)
    completed = []
    expected_evidence_ids = set()
    expected_unit_ids = set()
    for task in shard_tasks:
        ordinal = int(task.scope["source_sections"]["source"][0])
        evidence_id = f"transcript-{ordinal}"
        unit_id = f"unit-{ordinal}"
        expected_evidence_ids.add(evidence_id)
        expected_unit_ids.add(unit_id)
        lease = workspace.lease_work_item(task.id, owner="codex")
        result = _result(
            task_id=task.id,
            lease_token=lease.token,
            snapshot_digest=task.snapshot_digest,
            evidence_id=evidence_id,
            integrated=False,
            unit_id=unit_id,
            start=float((ordinal - 1) * 120),
        )
        result_path = lease.output_directory / "result.json"
        atomic_write_json(result_path, result)
        completed.append(submit_analyze_result(workspace, task.id, result_path))

    integration = plan_analyze_integration_task(workspace, run, completed)
    packet = json.loads((workspace.root / integration.packet_path).read_text(encoding="utf-8"))[
        "payload"
    ]

    assert set(packet["allowed_evidence_ids"]) == expected_evidence_ids
    assert set(packet["allowed_evidence_by_source"]["source"]) == expected_evidence_ids
    assert set(packet["required_semantic_unit_ids"]) == expected_unit_ids

    lease = workspace.lease_work_item(integration.id, owner="codex")
    forged = _result(
        task_id=integration.id,
        lease_token=lease.token,
        snapshot_digest=integration.snapshot_digest,
        evidence_id="invented-evidence",
        integrated=True,
        unit_id=next(iter(expected_unit_ids)),
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, forged)

    with pytest.raises(ProcessingError, match="outside its Analyze packet"):
        submit_analyze_result(workspace, integration.id, result_path)


def test_analyze_result_file_must_belong_to_task_output(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    run = workspace.create_analysis_run()
    [task] = plan_analyze_tasks(workspace, run)
    lease = workspace.lease_work_item(task.id, owner="codex")
    result = _result(
        task_id=task.id,
        lease_token=lease.token,
        snapshot_digest=task.snapshot_digest,
    )
    outside = tmp_path / "result.json"
    atomic_write_json(outside, result)

    with pytest.raises(ProcessingError, match="task output directory"):
        submit_analyze_result(workspace, task.id, outside)
