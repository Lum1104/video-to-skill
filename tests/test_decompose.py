from __future__ import annotations

from pathlib import Path

import pytest
from test_author import _analyzed_workspace

from video_to_skill.decompose import (
    SkillCandidate,
    SkillDecompositionResult,
    SkillPackPlan,
    apply_skill_pack_selection,
    load_canonical_skill_pack_checkpoint,
    plan_decompose_task,
    selected_skill_candidates,
    submit_decompose_result,
)
from video_to_skill.errors import ProcessingError
from video_to_skill.models import ObservationProducer
from video_to_skill.utils import atomic_write_json
from video_to_skill.work import WorkState


def _single_skill_plan() -> SkillPackPlan:
    return SkillPackPlan(
        decomposition_mode="single",
        rationale="All semantic units belong to one cohesive decision-making Skill.",
        skills=[
            SkillCandidate(
                id="evidence-conviction",
                name="evidence-conviction",
                title="Evidence-Updated Conviction",
                description="Make calibrated decisions by separating conviction from evidence.",
                rationale="Single unified mental model.",
                semantic_unit_ids=["unit-conviction"],
            )
        ],
    )


def _multi_skill_plan() -> SkillPackPlan:
    return SkillPackPlan(
        decomposition_mode="multi",
        rationale="The source teaches two independently triggerable workflows.",
        skills=[
            SkillCandidate(
                id="conviction-calibration",
                name="conviction-calibration",
                title="Conviction Calibration",
                description="Calibrate belief strength against incoming evidence.",
                rationale="Independent decision-hygiene workflow.",
                semantic_unit_ids=["unit-conviction"],
            ),
            SkillCandidate(
                id="decision-postmortem",
                name="decision-postmortem",
                title="Decision Postmortem",
                description="Review prior decisions by separating process quality from outcomes.",
                rationale="Independent retrospective workflow sharing core concept.",
                semantic_unit_ids=["unit-conviction"],
                prerequisites=["conviction-calibration"],
                estimated_scope="focused",
            ),
        ],
    )


def test_single_skill_decomposition_auto_selects(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = plan_decompose_task(workspace, run, analyze_task=analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")

    result = SkillDecompositionResult(
        task_id=task.id,
        lease_token=lease.token,
        snapshot_digest=task.snapshot_digest,
        producer=ObservationProducer(name="decompose-worker", run_id="decompose-run"),
        plan=_single_skill_plan(),
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    accepted = submit_decompose_result(workspace, task.id, result_path)

    assert accepted.state == WorkState.COMPLETE
    checkpoint = load_canonical_skill_pack_checkpoint(workspace)
    assert checkpoint.plan.decomposition_mode == "single"
    assert checkpoint.selection is not None
    assert checkpoint.selection.source == "auto"
    assert checkpoint.selection.selected_skill_ids == ["evidence-conviction"]
    assert [s.id for s in selected_skill_candidates(workspace)] == ["evidence-conviction"]


def test_multi_skill_decomposition_waits_for_selection_and_isolates_scopes(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = plan_decompose_task(workspace, run, analyze_task=analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")

    result = SkillDecompositionResult(
        task_id=task.id,
        lease_token=lease.token,
        snapshot_digest=task.snapshot_digest,
        producer=ObservationProducer(name="decompose-worker", run_id="decompose-run"),
        plan=_multi_skill_plan(),
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)
    submit_decompose_result(workspace, task.id, result_path)

    checkpoint = load_canonical_skill_pack_checkpoint(workspace)
    assert checkpoint.plan.decomposition_mode == "multi"
    assert checkpoint.selection is None
    with pytest.raises(ProcessingError, match="not been confirmed"):
        selected_skill_candidates(workspace)

    apply_skill_pack_selection(
        workspace,
        plan_task_id=task.id,
        selected_skill_ids=["conviction-calibration", "decision-postmortem"],
        source="user",
    )
    selected = selected_skill_candidates(workspace)
    assert [s.id for s in selected] == ["conviction-calibration", "decision-postmortem"]

    # Each decomposed skill gets an isolated workspace view sharing upstream Analyze records.
    view_a = workspace.for_skill("conviction-calibration")
    view_b = workspace.for_skill("decision-postmortem")
    assert view_a.edition_id != view_b.edition_id
    assert view_a.canonical_record("semantic-map") == workspace.canonical_record("semantic-map")
    assert view_b.canonical_record("semantic-map") == workspace.canonical_record("semantic-map")
    assert view_a.canonical_record("skill-pack-plan") == workspace.canonical_record(
        "skill-pack-plan"
    )

    run_a = view_a.create_analysis_run(analyze_task.snapshot_digest)
    task_a = view_a.ensure_work_item(
        run_id=run_a.id,
        role=task.role,
        scope={"kind": "course-authoring", "skill_id": "conviction-calibration"},
        persona_hint="Skill author.",
        packet={},
        result_schema={"type": "object"},
        snapshot_digest=run_a.snapshot_digest,
    )
    dummy_a = view_a.analysis_dir / "course-a.json"
    atomic_write_json(dummy_a, {"name": "conviction-calibration"})
    view_a.publish_canonical_record(
        kind="course",
        record_id="default",
        source_path=dummy_a,
        producer_task_id=task_a.id,
        snapshot_digest=task_a.snapshot_digest,
    )
    assert view_a.canonical_record("course") is not None
    assert view_b.canonical_record("course") is None
    assert workspace.canonical_record("course") is None


def test_decomposition_rejects_unknown_semantic_unit(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = plan_decompose_task(workspace, run, analyze_task=analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")

    bad_plan = SkillPackPlan(
        decomposition_mode="single",
        rationale="Invalid unit reference.",
        skills=[
            SkillCandidate(
                id="bad-skill",
                name="bad-skill",
                title="Bad Skill",
                description="References nonexistent unit.",
                rationale="Test error handling.",
                semantic_unit_ids=["unit-does-not-exist"],
            )
        ],
    )
    result = SkillDecompositionResult(
        task_id=task.id,
        lease_token=lease.token,
        snapshot_digest=task.snapshot_digest,
        producer=ObservationProducer(name="decompose-worker", run_id="decompose-run"),
        plan=bad_plan,
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    with pytest.raises(ProcessingError, match="unknown semantic unit ids"):
        submit_decompose_result(workspace, task.id, result_path)
