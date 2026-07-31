from __future__ import annotations

from pathlib import Path

import pytest

from video_to_skill.analyze import plan_analyze_tasks, submit_analyze_result
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import SemanticUnit
from video_to_skill.models import (
    ObservationProducer,
    SemanticSegment,
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
)
from video_to_skill.orchestration import (
    AnalyzeResult,
    CapabilityEvidence,
    SemanticCoverage,
)
from video_to_skill.utils import atomic_write_json
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


def _result(
    *,
    task_id: str,
    lease_token: str,
    snapshot_digest: str,
    evidence_id: str = "transcript-1",
    integrated: bool = True,
) -> AnalyzeResult:
    unit = SemanticUnit(
        id="unit-principle",
        source_id="source",
        start=0,
        end=60,
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


def test_long_course_analyze_tasks_are_bounded_section_groups(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, sections=25)
    run = workspace.create_analysis_run()

    tasks = plan_analyze_tasks(workspace, run)

    assert len(tasks) == 4
    assert all(task.scope["kind"] == "section-group" for task in tasks)
    assert max(
        len(task.scope["source_sections"]["source"])
        for task in tasks
    ) <= 8


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
