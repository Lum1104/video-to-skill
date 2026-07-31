"""Independent Review task planning, acceptance, and repair planning."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.author import AUTHOR_PERSONA
from video_to_skill.curriculum import (
    load_canonical_curriculum_checkpoint,
    load_canonical_curriculum_plan,
    load_canonical_curriculum_selection,
    validate_artifact_bound_curriculum,
    validate_curriculum_checkpoint_author_binding,
)
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import CurriculumDesign
from video_to_skill.orchestration import ArtifactDraftSpec, AuthorResult, ReviewResult
from video_to_skill.utils import atomic_write_json, hash_file, stable_hash
from video_to_skill.visual_assets import (
    canonical_visual_asset_candidates,
    visual_asset_candidate_packet,
)
from video_to_skill.work import AnalysisRun, WorkItem, WorkRole, WorkState
from video_to_skill.workspace import Workspace

REVIEW_PERSONA = (
    "You are a senior independent Agent Skill critic and learning-product evaluator with "
    "deep expertise in evidence grounding, semantic retention, instructional design, "
    "progressive disclosure, behavior testing, safety, and operational usability. Audit the "
    "actual canonical drafts and report concise findings without reconstructing the author's "
    "private reasoning."
)

MAX_REPAIR_CYCLES = 3

_REVIEW_RECORD_KINDS = (
    "semantic-map",
    "semantic-relations",
    "semantic-coverage",
    "course",
    "curriculum",
    "interaction",
    "capability-profile",
    "artifact-plan",
    "instructional-affordances",
    "claims",
    "assets",
)


def _verified_canonical_json(workspace: Workspace, kind: str) -> object:
    record = workspace.canonical_record(kind)
    if record is None:
        raise ProcessingError(f"Review requires canonical {kind}")
    path = workspace.root / record.path
    try:
        if path.is_symlink() or not path.is_file() or hash_file(path) != record.digest:
            raise ProcessingError(f"Canonical Review record failed its digest check: {kind}")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Invalid canonical Review JSON {kind}: {exc}") from exc


def _review_snapshot(
    workspace: Workspace,
    *,
    expected_author_task_id: str | None = None,
) -> tuple[str, dict[str, object]]:
    records: dict[str, dict[str, str]] = {}
    for kind in _REVIEW_RECORD_KINDS:
        record = workspace.canonical_record(kind)
        if record is None:
            raise ProcessingError(f"Review requires canonical {kind}")
        records[kind] = {
            "path": str(record.path),
            "digest": record.digest,
            "producer_task_id": record.producer_task_id,
        }
    checkpoint = load_canonical_curriculum_checkpoint(workspace)
    if checkpoint is not None:
        bound_author = validate_curriculum_checkpoint_author_binding(workspace, checkpoint)
        if expected_author_task_id is not None and bound_author.id != expected_author_task_id:
            raise ProcessingError("Review target is not the canonical checkpoint-bound Author")
        try:
            curriculum = CurriculumDesign.model_validate(
                _verified_canonical_json(workspace, "curriculum")
            )
            artifact_values = _verified_canonical_json(workspace, "artifact-plan")
            if not isinstance(artifact_values, list):
                raise ProcessingError("Canonical artifact plan must be a JSON list")
            artifact_specs = [ArtifactDraftSpec.model_validate(item) for item in artifact_values]
        except PydanticValidationError as exc:
            raise ProcessingError(f"Invalid curriculum checkpoint projection: {exc}") from exc
        validate_artifact_bound_curriculum(checkpoint, curriculum, artifact_specs)
        for kind, record in (
            ("curriculum-options", checkpoint.plan_record),
            ("selected-curriculum", checkpoint.selection_record),
        ):
            records[kind] = {
                "path": str(record.path),
                "digest": record.digest,
                "producer_task_id": record.producer_task_id,
            }
    visual_candidates_record = workspace.canonical_record("visual-asset-candidates")
    if visual_candidates_record is not None:
        records["visual-asset-candidates"] = {
            "path": str(visual_candidates_record.path),
            "digest": visual_candidates_record.digest,
            "producer_task_id": visual_candidates_record.producer_task_id,
        }
    artifacts = _verified_canonical_json(workspace, "artifact-plan")
    if not isinstance(artifacts, list):
        raise ProcessingError("Canonical artifact plan must be a JSON list")
    draft_records: dict[str, dict[str, str]] = {}
    for artifact in artifacts:
        artifact_id = str(artifact["id"])
        record = workspace.canonical_record("artifact-draft", artifact_id)
        if record is None:
            raise ProcessingError(f"Review requires canonical draft for {artifact_id}")
        draft_records[artifact_id] = {
            "path": str(record.path),
            "digest": record.digest,
            "producer_task_id": record.producer_task_id,
        }
    records["artifact-drafts"] = {
        "path": "",
        "digest": stable_hash(draft_records, length=64),
        "producer_task_id": "",
    }
    visual_image_records = {
        candidate.candidate_id: {
            "path": str(image_path.relative_to(workspace.root)),
            "digest": candidate.sha256,
        }
        for candidate, image_path in canonical_visual_asset_candidates(workspace)
    }
    records["visual-asset-images"] = {
        "path": "",
        "digest": stable_hash(visual_image_records, length=64),
        "producer_task_id": "",
    }
    snapshot = stable_hash(
        {
            "records": records,
            "drafts": draft_records,
            "visual_asset_images": visual_image_records,
        },
        length=64,
    )
    return snapshot, {
        "records": records,
        "artifact_drafts": draft_records,
        "visual_asset_images": visual_image_records,
    }


def plan_review_task(
    workspace: Workspace,
    run: AnalysisRun,
    *,
    author_task: WorkItem,
    repair_cycle: int = 0,
    additional_dependencies: list[str] | None = None,
) -> WorkItem:
    if (
        author_task.role != WorkRole.AUTHOR
        or author_task.scope.get("kind") not in {"course-authoring", "course-authoring-repair"}
        or author_task.state != WorkState.COMPLETE
    ):
        raise ProcessingError("Review requires a completed Author task")
    if repair_cycle < 0 or repair_cycle > MAX_REPAIR_CYCLES:
        raise ProcessingError("Review repair cycle is outside the supported range")
    reviewed_snapshot, record_packet = _review_snapshot(
        workspace,
        expected_author_task_id=author_task.id,
    )
    packet = {
        "instructions": (
            "Independently audit source-meaning retention and instructional-affordance "
            "coverage, then grounding, disclosure, runtime behavior, safety, and scope. "
            "Inspect canonical files directly. Treat missing quick reference, operational "
            "validation or recovery, capstone scoring, progressive hints, retry, and teaching "
            "scaffolds as product losses when the claimed capability requires them. Inspect "
            "every selected teaching visual and audit necessity, legibility, surrounding "
            "context, privacy, evidence grounding, and whether artifacts load it only on demand."
        ),
        "reviewed_snapshot_digest": reviewed_snapshot,
        "canonical": record_packet,
        "visual_asset_candidates": visual_asset_candidate_packet(workspace),
        "author_task_id": author_task.id,
        "repair_cycle": repair_cycle,
    }
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.REVIEW,
        scope={
            "kind": "independent-review",
            "author_task_id": author_task.id,
            "reviewed_snapshot_digest": reviewed_snapshot,
            "repair_cycle": repair_cycle,
        },
        persona_hint=REVIEW_PERSONA,
        packet=packet,
        result_schema=ReviewResult.model_json_schema(mode="validation"),
        dependencies=[author_task.id, *(additional_dependencies or [])],
        snapshot_digest=run.snapshot_digest,
    )


def _load_review_result(path: Path) -> ReviewResult:
    try:
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ProcessingError("Review result must be a regular JSON file no larger than 4 MiB")
        return ReviewResult.model_validate_json(path.read_text(encoding="utf-8"))
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid Review result: {exc}") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Could not read Review result: {exc}") from exc


def submit_review_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> WorkItem:
    task = workspace.get_work_item(task_id)
    if task.role != WorkRole.REVIEW:
        raise ProcessingError(f"Task is not a Review task: {task_id}")
    result = _load_review_result(result_path)
    if result.task_id != task.id or result.snapshot_digest != task.snapshot_digest:
        raise ProcessingError("Review result does not belong to this task snapshot")
    expected_snapshot = str(task.scope["reviewed_snapshot_digest"])
    current_snapshot, _records = _review_snapshot(
        workspace,
        expected_author_task_id=str(task.scope["author_task_id"]),
    )
    if (
        result.reviewed_snapshot_digest != expected_snapshot
        or current_snapshot != expected_snapshot
    ):
        raise ProcessingError("Review result targets a stale canonical authoring snapshot")
    author_task_id = str(task.scope["author_task_id"])
    author_producer = workspace.work_result_producer(author_task_id)
    if author_producer is None:
        raise ProcessingError("Review cannot identify the canonical Author producer")
    reviewer = result.producer.model_dump(mode="json")
    same_name = reviewer.get("name") == author_producer.get("name")
    same_run = reviewer.get("run_id") is not None and reviewer.get("run_id") == author_producer.get(
        "run_id"
    )
    if same_name or same_run:
        raise ProcessingError("Review producer must be independent of the Author producer")
    curriculum_record = workspace.canonical_record("curriculum-options")
    if curriculum_record is not None:
        curriculum_producer = workspace.work_result_producer(curriculum_record.producer_task_id)
        if curriculum_producer is None:
            raise ProcessingError("Review cannot identify the curriculum Author producer")
        same_curriculum_name = reviewer.get("name") == curriculum_producer.get("name")
        same_curriculum_run = reviewer.get("run_id") is not None and reviewer.get(
            "run_id"
        ) == curriculum_producer.get("run_id")
        if same_curriculum_name or same_curriculum_run:
            raise ProcessingError(
                "Review producer must be independent of the curriculum Author producer"
            )
    output = workspace.tasks_dir / task.id / "output"
    review_path = output / "critic-report.json"
    behavior_path = output / "behavior-report.json"
    atomic_write_json(
        review_path,
        {
            "verdict": result.verdict,
            "reviewed_snapshot_digest": result.reviewed_snapshot_digest,
            "repair_cycle": task.scope["repair_cycle"],
            "findings": [finding.model_dump(mode="json") for finding in result.findings],
        },
    )
    atomic_write_json(
        behavior_path,
        {
            "passed": all(check.passed for check in result.behavior_checks),
            "checks": [check.model_dump(mode="json") for check in result.behavior_checks],
        },
    )
    accepted, _canonical = workspace.accept_work_result(
        task_id=task.id,
        lease_token=result.lease_token,
        result_path=result_path,
        producer=reviewer,
        canonical_outputs=[
            ("critic-report", "default", review_path),
            ("behavior-report", "default", behavior_path),
        ],
    )
    return accepted


def plan_author_repair_task(
    workspace: Workspace,
    run: AnalysisRun,
    *,
    failed_review_task: WorkItem,
    prior_author_task: WorkItem,
) -> WorkItem:
    if failed_review_task.role != WorkRole.REVIEW or failed_review_task.state != WorkState.COMPLETE:
        raise ProcessingError("Author repair requires a completed Review task")
    review_record = workspace.canonical_record("critic-report")
    if review_record is None or review_record.producer_task_id != failed_review_task.id:
        raise ProcessingError("Author repair requires the failed canonical critic report")
    try:
        report = json.loads((workspace.root / review_record.path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Invalid canonical critic report: {exc}") from exc
    if report.get("verdict") != "fail":
        raise ProcessingError("Passing reviews do not create Author repair tasks")
    if (
        prior_author_task.role != WorkRole.AUTHOR
        or prior_author_task.scope.get("kind")
        not in {"course-authoring", "course-authoring-repair"}
        or prior_author_task.state != WorkState.COMPLETE
    ):
        raise ProcessingError("Author repair requires a completed course Author task")
    repair_cycle = int(failed_review_task.scope["repair_cycle"]) + 1
    if repair_cycle > MAX_REPAIR_CYCLES:
        raise ProcessingError("Maximum Author repair cycles exceeded")
    if prior_author_task.result_path is None:
        raise ProcessingError("Author repair requires the prior accepted Author result")
    curriculum_scope: dict[str, object] = {}
    curriculum_packet: dict[str, dict[str, str]] = {}
    checkpoint_scope = {
        "curriculum_task_id": prior_author_task.scope.get("curriculum_task_id"),
        "curriculum_plan_digest": prior_author_task.scope.get("curriculum_plan_digest"),
        "curriculum_selection_digest": prior_author_task.scope.get("curriculum_selection_digest"),
        "curriculum_selection_producer_task_id": prior_author_task.scope.get(
            "curriculum_selection_producer_task_id"
        ),
    }
    present_checkpoint_values = [value is not None for value in checkpoint_scope.values()]
    if any(present_checkpoint_values) and not all(present_checkpoint_values):
        raise ProcessingError("Prior Author task has a partial curriculum binding")
    if all(present_checkpoint_values):
        curriculum_task_id = checkpoint_scope["curriculum_task_id"]
        expected_plan_digest = checkpoint_scope["curriculum_plan_digest"]
        selection_digest = checkpoint_scope["curriculum_selection_digest"]
        selection_producer = checkpoint_scope["curriculum_selection_producer_task_id"]
        if not all(
            isinstance(value, str)
            for value in (
                expected_plan_digest,
                selection_digest,
                selection_producer,
                curriculum_task_id,
            )
        ):
            raise ProcessingError("Prior Author task has an invalid curriculum binding")
        _plan, plan_record = load_canonical_curriculum_plan(
            workspace,
            expected_digest=str(expected_plan_digest),
            expected_producer_task_id=str(curriculum_task_id),
        )
        selection, selection_record = load_canonical_curriculum_selection(
            workspace,
            expected_digest=str(selection_digest),
            expected_producer_task_id=str(selection_producer),
        )
        if selection.curriculum_plan_digest != plan_record.digest:
            raise ProcessingError("Author repair curriculum selection targets different options")
        curriculum_scope = {
            "curriculum_task_id": str(curriculum_task_id),
            "curriculum_plan_digest": plan_record.digest,
            "curriculum_selection_digest": selection_record.digest,
            "curriculum_selection_producer_task_id": str(selection_producer),
        }
        curriculum_packet = {
            "curriculum-options": {
                "path": str(plan_record.path),
                "digest": plan_record.digest,
            },
            "selected-curriculum": {
                "path": str(selection_record.path),
                "digest": selection_record.digest,
            },
        }
    packet = {
        "instructions": (
            "Repair every blocking critic finding against the current canonical state. "
            "Write revised drafts only in this task's output directory and submit a complete "
            "Author result so publication remains atomic. Preserve unaffected grounded "
            "material and do not weaken capability claims merely to hide missing affordances. "
            "Reuse only verified visual asset candidates, keep indispensable images linked "
            "on demand, and remove decorative, illegible, private, or misleading selections. "
            "Preserve the pinned selected curriculum and do not reopen its user decision."
        ),
        "prior_author_result": str(prior_author_task.result_path),
        "critic_report": str(review_record.path),
        "repair_cycle": repair_cycle,
        "visual_asset_candidates": visual_asset_candidate_packet(workspace),
        "canonical_curriculum": curriculum_packet,
    }
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope={
            "kind": "course-authoring-repair",
            "revision": repair_cycle + 1,
            "repair_of": prior_author_task.id,
            "review_task_id": failed_review_task.id,
            **curriculum_scope,
        },
        persona_hint=AUTHOR_PERSONA,
        packet=packet,
        result_schema=AuthorResult.model_json_schema(mode="validation"),
        dependencies=[failed_review_task.id],
        snapshot_digest=run.snapshot_digest,
    )
