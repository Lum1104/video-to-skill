from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import CurriculumDesign, CurriculumPath
from video_to_skill.models import ObservationProducer
from video_to_skill.orchestration import (
    ArtifactDraftSpec,
    BehaviorCheck,
    CurriculumPlan,
    CurriculumPlanPath,
    CurriculumSelection,
    ReviewFinding,
    ReviewResult,
)
from video_to_skill.review import (
    plan_author_repair_task,
    plan_review_task,
    submit_review_result,
)
from video_to_skill.utils import atomic_write_json, hash_file
from video_to_skill.work import WorkRole, WorkState
from video_to_skill.workspace import Workspace


def _authored_workspace(tmp_path: Path) -> tuple[Workspace, object, object]:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    run = workspace.create_analysis_run()
    curriculum = workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope={"kind": "curriculum-planning"},
        persona_hint="Principal curriculum architect.",
        packet={},
        result_schema={"type": "object"},
        snapshot_digest=run.snapshot_digest,
    )
    curriculum_lease = workspace.lease_work_item(curriculum.id, owner="codex")
    curriculum_result = curriculum_lease.output_directory / "result.json"
    atomic_write_json(curriculum_result, {"curriculum": "curriculum-worker"})
    options_path = curriculum_lease.output_directory / "curriculum-options.json"
    atomic_write_json(
        options_path,
        CurriculumPlan(
            recommended_path_id="thematic",
            rationale="Use the thematic path.",
            paths=[
                CurriculumPlanPath(
                    id="thematic",
                    title="Course",
                    kind="thematic",
                    use_when="learning the course",
                    unit_sequence=["unit"],
                )
            ],
        ),
    )
    selection_path = curriculum_lease.output_directory / "selected-curriculum.json"
    atomic_write_json(
        selection_path,
        CurriculumSelection(
            curriculum_plan_digest=hash_file(options_path),
            selected_path_id="thematic",
            source="recommended",
        ),
    )
    curriculum, curriculum_records = workspace.accept_work_result(
        task_id=curriculum.id,
        lease_token=curriculum_lease.token,
        result_path=curriculum_result,
        producer=ObservationProducer(
            name="curriculum-worker",
            run_id="curriculum-run",
        ).model_dump(mode="json"),
        canonical_outputs=[
            ("curriculum-options", "default", options_path),
            ("selected-curriculum", "default", selection_path),
        ],
    )
    records_by_kind = {record.kind: record for record in curriculum_records}
    author = workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope={
            "kind": "course-authoring",
            "revision": 1,
            "curriculum_task_id": curriculum.id,
            "curriculum_plan_digest": records_by_kind["curriculum-options"].digest,
            "curriculum_selection_digest": records_by_kind["selected-curriculum"].digest,
            "curriculum_selection_producer_task_id": curriculum.id,
        },
        persona_hint="Principal curriculum architect.",
        packet={},
        result_schema={"type": "object"},
        dependencies=[curriculum.id],
        snapshot_digest=run.snapshot_digest,
    )
    lease = workspace.lease_work_item(author.id, owner="codex")
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, {"author": "author-worker"})
    draft = lease.output_directory / "course.md"
    draft.write_text("# Course\n", encoding="utf-8")
    artifact = ArtifactDraftSpec(
        id="artifact",
        path="chapters/course.md",
        title="Course",
        modes=["learn"],
        disclosure="normal",
        use_when="learning the course",
        independent_loading_reason="Load the course as one coherent guide.",
        semantic_unit_ids=["unit"],
        draft_path=draft.name,
        draft_sha256=hash_file(draft),
    )
    values = {
        "semantic-map": [{"id": "unit"}],
        "semantic-relations": [],
        "semantic-coverage": {"material_units_accounted_for": True},
        "course": {"name": "course"},
        "curriculum": CurriculumDesign(
            selected_path_id="thematic",
            rationale="Use the thematic path.",
            paths=[
                CurriculumPath(
                    id="thematic",
                    title="Course",
                    kind="thematic",
                    use_when="learning the course",
                    artifact_ids=[artifact.id],
                )
            ],
        ).model_dump(mode="json"),
        "interaction": {"welcome": "start"},
        "capability-profile": {"learn": "light"},
        "artifact-plan": [artifact.model_dump(mode="json")],
        "instructional-affordances": [{"id": "affordance"}],
        "claims": [{"id": "claim"}],
        "assets": [],
    }
    outputs = []
    for kind, value in values.items():
        path = lease.output_directory / f"{kind}.json"
        atomic_write_json(path, value)
        outputs.append((kind, "default", path))
    outputs.append(("artifact-draft", "artifact", draft))
    author, _records = workspace.accept_work_result(
        task_id=author.id,
        lease_token=lease.token,
        result_path=result_path,
        producer=ObservationProducer(
            name="author-worker",
            run_id="author-run",
        ).model_dump(mode="json"),
        canonical_outputs=outputs,
    )
    return workspace, run, author


def _review_result(
    *,
    task_id: str,
    lease_token: str,
    snapshot_digest: str,
    reviewed_snapshot_digest: str,
    reviewer_name: str = "review-worker",
    verdict: str = "pass",
) -> ReviewResult:
    findings = []
    checks = [
        BehaviorCheck(
            id="empty-invocation",
            scenario="Invoke the generated Skill without a request.",
            passed=verdict == "pass",
            summary="The Skill waits without side effects."
            if verdict == "pass"
            else "It loads files.",
        )
    ]
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
        task_id=task_id,
        lease_token=lease_token,
        snapshot_digest=snapshot_digest,
        reviewed_snapshot_digest=reviewed_snapshot_digest,
        producer=ObservationProducer(name=reviewer_name, run_id="review-run"),
        verdict=verdict,
        findings=findings,
        behavior_checks=checks,
    )


def test_independent_review_persists_reports(tmp_path: Path) -> None:
    workspace, run, author = _authored_workspace(tmp_path)
    review = plan_review_task(workspace, run, author_task=author)
    lease = workspace.lease_work_item(review.id, owner="codex")
    result = _review_result(
        task_id=review.id,
        lease_token=lease.token,
        snapshot_digest=review.snapshot_digest,
        reviewed_snapshot_digest=str(review.scope["reviewed_snapshot_digest"]),
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    accepted = submit_review_result(workspace, review.id, result_path)

    assert accepted.state == WorkState.COMPLETE
    assert workspace.canonical_record("critic-report").producer_task_id == review.id
    assert workspace.canonical_record("behavior-report") is not None


def test_review_rejects_author_as_reviewer(tmp_path: Path) -> None:
    workspace, run, author = _authored_workspace(tmp_path)
    review = plan_review_task(workspace, run, author_task=author)
    lease = workspace.lease_work_item(review.id, owner="codex")
    result = _review_result(
        task_id=review.id,
        lease_token=lease.token,
        snapshot_digest=review.snapshot_digest,
        reviewed_snapshot_digest=str(review.scope["reviewed_snapshot_digest"]),
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
    review = plan_review_task(workspace, run, author_task=author)
    lease = workspace.lease_work_item(review.id, owner="codex")
    result = _review_result(
        task_id=review.id,
        lease_token=lease.token,
        snapshot_digest=review.snapshot_digest,
        reviewed_snapshot_digest=str(review.scope["reviewed_snapshot_digest"]),
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
