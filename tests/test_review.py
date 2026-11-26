from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_author import _analyzed_workspace, _author_result, _plan_course_author_task

from video_to_skill.agentic import visual_retention_gaps
from video_to_skill.author import submit_author_result
from video_to_skill.errors import ProcessingError
from video_to_skill.models import (
    ObservationProducer,
    VisualRetentionInterval,
    VisualRetentionReport,
    WarningRecord,
)
from video_to_skill.orchestration import (
    ReviewFinding,
    ReviewResult,
)
from video_to_skill.review import (
    plan_author_repair_task,
    plan_review_task,
    submit_review_result,
)
from video_to_skill.utils import atomic_write_json
from video_to_skill.work import AnalysisRun, WorkItem, WorkRole, WorkState
from video_to_skill.workspace import Workspace


def _authored_workspace(tmp_path: Path) -> tuple[Workspace, AnalysisRun, WorkItem]:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    author = _plan_course_author_task(workspace, run, analyze_task)
    lease = workspace.lease_work_item(author.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_text(
        "# Evidence-Updated Conviction\n\nUse retrieval, practice, application, and reference.\n",
        encoding="utf-8",
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, _author_result(author, lease.token, draft))
    accepted = submit_author_result(workspace, author.id, result_path)
    return workspace, run, accepted


def _plan_review(
    workspace: Workspace,
    run: AnalysisRun,
    author: WorkItem,
) -> WorkItem:
    return plan_review_task(
        workspace,
        run,
        author_task=author,
    )


def _review_result(
    workspace: Workspace,
    task: WorkItem,
    lease_token: str,
    *,
    reviewer_name: str = "review-worker",
    verdict: str = "pass",
) -> ReviewResult:
    task = workspace.get_work_item(task.id)
    findings = []
    if verdict == "fail":
        findings.append(
            ReviewFinding(
                id="missing-capstone",
                category="instructional-affordance",
                severity="error",
                target_kind="artifact-plan",
                target_id="artifact",
                summary="Strong practice lacks a scored capstone and retry loop.",
                required_change="Add an integrated capstone with scoring, hints, and retry.",
            )
        )
    return ReviewResult(
        task_id=task.id,
        lease_token=lease_token,
        execution_context_id=str(task.execution_context_id),
        snapshot_digest=task.snapshot_digest,
        reviewed_snapshot_digest=str(task.scope["reviewed_snapshot_digest"]),
        producer=ObservationProducer(name=reviewer_name, run_id="review-run"),
        target_build_id=str(task.scope["target_build_id"]),
        target_content_digest=str(task.scope["target_content_digest"]),
        verdict=verdict,
        findings=findings,
    )


def test_independent_review_persists_critic_report(tmp_path: Path) -> None:
    workspace, run, author = _authored_workspace(tmp_path)
    review = _plan_review(workspace, run, author)
    lease = workspace.lease_work_item(review.id, owner="codex")
    result = _review_result(workspace, review, lease.token)
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    accepted = submit_review_result(workspace, review.id, result_path)

    assert accepted.state == WorkState.COMPLETE
    assert workspace.canonical_record("critic-report").producer_task_id == review.id
    assert workspace.canonical_record("behavior-report") is None


def test_review_rejects_wrong_engine_execution_context(tmp_path: Path) -> None:
    workspace, run, author = _authored_workspace(tmp_path)
    review = _plan_review(workspace, run, author)
    lease = workspace.lease_work_item(review.id, owner="codex")
    payload = _review_result(workspace, review, lease.token).model_dump(mode="json")
    payload["execution_context_id"] = f"ctx-{'x' * 24}"
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, payload)

    with pytest.raises(ProcessingError, match="does not belong to this task snapshot"):
        submit_review_result(workspace, review.id, result_path)


def test_review_packet_binds_exact_analysis_depth_contract(tmp_path: Path) -> None:
    workspace, run, author = _authored_workspace(tmp_path)
    contract = workspace.load_manifest().analysis_depth
    assert contract is not None
    retention = VisualRetentionReport(
        source_id="source",
        budget_digest=contract.budget_digest,
        candidate_count=9,
        retained_count=4,
        dropped_count=5,
        truncated=True,
        affected_intervals=[VisualRetentionInterval(start=10, end=20, dropped_count=5)],
    )
    workspace.save_visual_retention_report(retention)
    workspace.upsert_gaps(visual_retention_gaps(retention))
    workspace.add_warning(
        WarningRecord(
            code="visual-retention-truncated",
            message="Visual coverage is partial because five candidates were dropped.",
            source_id="source",
        )
    )
    review = _plan_review(workspace, run, author)
    packet = json.loads((workspace.root / review.packet_path).read_text(encoding="utf-8"))[
        "payload"
    ]
    assert packet["analysis_depth_contract"] == contract.model_dump(mode="json")
    assert (
        packet["analysis_depth_contract_digest"] == review.scope["analysis_depth_contract_digest"]
    )
    assert packet["visual_retention"][0]["dropped_count"] == 5
    assert packet["visual_retention_gaps"][0]["gap_type"] == "visual-retention-truncated"
    assert packet["visual_retention_warnings"][0]["code"] == "visual-retention-truncated"


def test_review_preview_cache_is_revalidated_against_fresh_render(tmp_path: Path) -> None:
    workspace, run, author = _authored_workspace(tmp_path)
    review = _plan_review(workspace, run, author)
    packet = json.loads((workspace.root / review.packet_path).read_text(encoding="utf-8"))[
        "payload"
    ]
    target = workspace.root / packet["evaluation_target"]["path"]
    (target / "SKILL.md").write_text("preseeded or stale bytes", encoding="utf-8")

    with pytest.raises(ProcessingError, match="differs from a fresh canonical engine render"):
        plan_review_task(workspace, run, author_task=author)


def test_review_preview_rejects_symlinked_private_root(tmp_path: Path) -> None:
    workspace, run, author = _authored_workspace(tmp_path)
    outside = tmp_path / "outside-review-targets"
    outside.mkdir()
    (workspace.analysis_dir / "review-targets").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(ProcessingError, match="symlinked path components"):
        plan_review_task(workspace, run, author_task=author)


def test_review_rejects_author_as_reviewer(tmp_path: Path) -> None:
    workspace, run, author = _authored_workspace(tmp_path)
    review = _plan_review(workspace, run, author)
    lease = workspace.lease_work_item(review.id, owner="codex")
    result = _review_result(
        workspace,
        review,
        lease.token,
        reviewer_name="author-worker",
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    with pytest.raises(ProcessingError, match="independent"):
        submit_review_result(workspace, review.id, result_path)


def test_review_rejects_tampered_curriculum_checkpoint_bytes(tmp_path: Path) -> None:
    workspace, run, author = _authored_workspace(tmp_path)
    options_record = workspace.canonical_record("curriculum-options")
    assert options_record is not None
    options_path = workspace.root / options_record.path
    options_path.write_bytes(options_path.read_bytes() + b"\n")

    with pytest.raises(ProcessingError, match="curriculum plan failed its digest check"):
        plan_review_task(workspace, run, author_task=author)


def test_review_rejects_selection_changed_after_authoring(tmp_path: Path) -> None:
    workspace, run, author = _authored_workspace(tmp_path)
    options_record = workspace.canonical_record("curriculum-options")
    selection_record = workspace.canonical_record("selected-curriculum")
    assert options_record is not None
    assert selection_record is not None
    selection = json.loads((workspace.root / selection_record.path).read_text(encoding="utf-8"))
    drift_path = (
        workspace.tasks_dir / options_record.producer_task_id / "output" / "selection-drift.json"
    )
    drift_path.write_text(json.dumps(selection, separators=(",", ":")), encoding="utf-8")
    workspace.publish_canonical_record(
        kind="selected-curriculum",
        record_id="default",
        source_path=drift_path,
        producer_task_id=options_record.producer_task_id,
        snapshot_digest=selection_record.snapshot_digest,
    )

    with pytest.raises(ProcessingError, match="not bound to the current curriculum checkpoint"):
        plan_review_task(workspace, run, author_task=author)


def test_failed_review_creates_new_author_revision_task(tmp_path: Path) -> None:
    workspace, run, author = _authored_workspace(tmp_path)
    review = _plan_review(workspace, run, author)
    lease = workspace.lease_work_item(review.id, owner="codex")
    result = _review_result(
        workspace,
        review,
        lease.token,
        verdict="fail",
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)
    failed_review = submit_review_result(workspace, review.id, result_path)

    repair = plan_author_repair_task(
        workspace,
        run,
        failed_review_task=failed_review,
        prior_author_task=author,
    )

    assert failed_review.state == WorkState.COMPLETE
    assert repair.role == WorkRole.AUTHOR
    assert repair.dependencies == [failed_review.id]
    assert repair.scope["revision"] == 2
    assert repair.scope["curriculum_plan_digest"] == author.scope["curriculum_plan_digest"]
    assert (
        repair.scope["curriculum_selection_digest"] == author.scope["curriculum_selection_digest"]
    )
    assert (
        repair.scope["artifact_language_contract_digest"]
        == author.scope["artifact_language_contract_digest"]
    )
    assert (
        repair.scope["artifact_language_declaration_digest"]
        == author.scope["artifact_language_declaration_digest"]
    )
