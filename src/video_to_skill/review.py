"""Independent Review task planning, acceptance, and repair planning."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.analysis_depth import verify_analysis_depth_contract
from video_to_skill.artifact_language import (
    canonical_artifact_language_state,
    ensure_artifact_language_contract,
)
from video_to_skill.author import AUTHOR_PERSONA
from video_to_skill.behavior import (
    BehaviorTargetSnapshot,
    snapshot_behavior_target,
)
from video_to_skill.compiler import assemble_workspace_blueprint
from video_to_skill.curriculum import (
    load_canonical_curriculum_plan,
    load_canonical_curriculum_selection,
)
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import (
    COURSE_SKILL_RENDERER_CONTRACT_VERSION,
    course_skill_build_id,
    render_course_skill_review_target,
)
from video_to_skill.models import EvidenceGapType
from video_to_skill.orchestration import (
    AuthorResult,
    CriticReport,
    ReviewResult,
)
from video_to_skill.review_contract import (
    reconstruct_critic_report,
    reconstruct_review_snapshot,
    verified_canonical_review_json,
)
from video_to_skill.utils import hash_file, stable_hash
from video_to_skill.visual_assets import visual_asset_candidate_packet
from video_to_skill.work import AnalysisRun, WorkItem, WorkRole, WorkState
from video_to_skill.workspace import Workspace

REVIEW_PERSONA = (
    "Independent Skill reviewer. Audit the canonical drafts and rendered Skill preview "
    "for evidence grounding, coverage, disclosure, safety, and usability."
)

MAX_REPAIR_CYCLES = 2


@dataclass(frozen=True)
class ReviewTargetPlan:
    reviewed_snapshot_digest: str
    canonical_packet: dict[str, object]
    target: BehaviorTargetSnapshot


def _verified_canonical_json(workspace: Workspace, kind: str) -> object:
    return verified_canonical_review_json(workspace, kind)


def _review_snapshot(
    workspace: Workspace,
    *,
    expected_author_task_id: str | None = None,
) -> tuple[str, dict[str, object]]:
    return reconstruct_review_snapshot(
        workspace,
        expected_author_task_id=expected_author_task_id,
    )


def _verified_task_packet(workspace: Workspace, task: WorkItem) -> dict[str, object]:
    try:
        packet = json.loads((workspace.root / task.packet_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Invalid Review task packet: {exc}") from exc
    if not isinstance(packet, dict) or stable_hash(packet, length=64) != task.packet_digest:
        raise ProcessingError("Review task packet failed its digest check")
    return packet


def _review_target_plan(
    workspace: Workspace,
    *,
    author_task: WorkItem,
) -> ReviewTargetPlan:
    reviewed_snapshot, canonical_packet = _review_snapshot(
        workspace,
        expected_author_task_id=author_task.id,
    )
    projection = assemble_workspace_blueprint(workspace)
    build_id = course_skill_build_id(projection.blueprint)
    target_key = stable_hash(
        {
            "renderer_contract": COURSE_SKILL_RENDERER_CONTRACT_VERSION,
            "reviewed_snapshot": reviewed_snapshot,
            "build_id": build_id,
        },
        length=32,
    )
    target_root = workspace.analysis_dir / "review-targets"
    target_path = target_root / target_key
    workspace.ensure_directory(target_root)
    fresh_path = target_path.with_name(f".{target_path.name}-fresh-{secrets.token_hex(12)}")
    try:
        render_course_skill_review_target(
            projection.blueprint,
            fresh_path,
            workspace_root=workspace.root,
            allowed_root=target_root,
        )
        fresh = snapshot_behavior_target(
            fresh_path,
            expected_build_id=build_id,
            allowed_root=target_root,
        )
        if target_path.is_symlink():
            raise ProcessingError("Cached review target cannot be a symlink")
        if target_path.exists():
            existing = snapshot_behavior_target(
                target_path,
                expected_build_id=build_id,
                allowed_root=target_root,
            )
            if existing.content_digest != fresh.content_digest or existing.files != fresh.files:
                raise ProcessingError(
                    "Cached review target differs from a fresh canonical engine render"
                )
        else:
            os.replace(fresh_path, target_path)
    finally:
        if fresh_path.exists() or fresh_path.is_symlink():
            if fresh_path.is_dir() and not fresh_path.is_symlink():
                shutil.rmtree(fresh_path)
            else:
                fresh_path.unlink(missing_ok=True)
    target = snapshot_behavior_target(
        target_path,
        expected_build_id=build_id,
        allowed_root=target_root,
    )
    return ReviewTargetPlan(
        reviewed_snapshot_digest=reviewed_snapshot,
        canonical_packet=canonical_packet,
        target=target,
    )


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
    plan = _review_target_plan(workspace, author_task=author_task)
    language_state = canonical_artifact_language_state(
        workspace,
        expected_author_task_id=author_task.id,
    )
    analysis_depth = workspace.analysis_depth_contract()
    if analysis_depth is None:
        raise ProcessingError("Review requires a persisted analysis-depth contract")
    verify_analysis_depth_contract(analysis_depth)
    analysis_depth_digest = stable_hash(
        analysis_depth.model_dump(mode="json"),
        length=64,
    )
    packet = {
        "instructions": (
            "Independently audit the rendered Skill preview and canonical records for "
            "evidence grounding, source-meaning retention, instructional usefulness, "
            "disclosure boundaries, visual asset quality, safety, and scope. "
            f"Verify all authored prose matches the canonical artifact language "
            f"{language_state.declaration.artifact_language!r}. "
            "Return pass unless there is a concrete blocking defect."
        ),
        "artifact_language_contract": language_state.contract.model_dump(mode="json"),
        "artifact_language_contract_digest": language_state.contract_digest,
        "artifact_language_declaration": language_state.declaration.model_dump(mode="json"),
        "artifact_language_declaration_digest": language_state.declaration_digest,
        "analysis_depth_contract": analysis_depth.model_dump(mode="json"),
        "analysis_depth_contract_digest": analysis_depth_digest,
        "reviewed_snapshot_digest": plan.reviewed_snapshot_digest,
        "canonical": plan.canonical_packet,
        "evaluation_target": {
            "path": str(plan.target.path.relative_to(workspace.root)),
            "build_id": plan.target.build_id,
            "content_digest": plan.target.content_digest,
            "renderer_contract_version": COURSE_SKILL_RENDERER_CONTRACT_VERSION,
        },
        "visual_asset_candidates": visual_asset_candidate_packet(workspace),
        "visual_retention": [
            report.model_dump(mode="json") for report in workspace.visual_retention_reports()
        ],
        "visual_retention_gaps": [
            gap.model_dump(mode="json")
            for gap in workspace.gaps(
                gap_type=EvidenceGapType.VISUAL_RETENTION_TRUNCATED,
                resolved=None,
                limit=None,
            )
        ],
        "visual_retention_warnings": [
            warning.model_dump(mode="json")
            for warning in workspace.list_warnings()
            if warning.code == "visual-retention-truncated"
        ],
        "author_task_id": author_task.id,
        "repair_cycle": repair_cycle,
    }
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.REVIEW,
        scope={
            "kind": "independent-review",
            "renderer_contract_version": COURSE_SKILL_RENDERER_CONTRACT_VERSION,
            "author_task_id": author_task.id,
            "reviewed_snapshot_digest": plan.reviewed_snapshot_digest,
            "target_build_id": plan.target.build_id,
            "target_content_digest": plan.target.content_digest,
            "repair_cycle": repair_cycle,
            "artifact_language_contract_digest": language_state.contract_digest,
            "artifact_language_declaration_digest": language_state.declaration_digest,
            "analysis_depth_contract_digest": analysis_depth_digest,
        },
        persona_hint=REVIEW_PERSONA,
        packet=packet,
        result_schema=ReviewResult.model_json_schema(mode="validation"),
        dependencies=[
            author_task.id,
            *(additional_dependencies or []),
        ],
        snapshot_digest=run.snapshot_digest,
    )


def _load_review_result(payload: bytes) -> ReviewResult:
    try:
        return ReviewResult.model_validate_json(payload.decode("utf-8"))
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid Review result: {exc}") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Could not read Review result: {exc}") from exc


def submit_review_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> WorkItem:
    task = workspace.get_work_item(task_id)
    if task.role != WorkRole.REVIEW or task.scope.get("kind") != "independent-review":
        raise ProcessingError(f"Task is not a Review task: {task_id}")
    packet = _verified_task_packet(workspace, task)
    result_snapshot = workspace.task_output_file_snapshot(
        task_id,
        result_path,
        max_bytes=4 * 1024 * 1024,
    )
    result = _load_review_result(result_snapshot.payload)
    if (
        result.task_id != task.id
        or result.snapshot_digest != task.snapshot_digest
        or result.execution_context_id != task.execution_context_id
    ):
        raise ProcessingError("Review result does not belong to this task snapshot")
    author_task_id = str(task.scope["author_task_id"])
    author_task = workspace.get_work_item(author_task_id)
    if task.execution_context_id is None:
        raise ProcessingError("Independent Review is missing its engine-issued execution context")
    if author_task.execution_context_id == task.execution_context_id:
        raise ProcessingError("Review and Author require distinct engine-issued contexts")
    plan = _review_target_plan(workspace, author_task=author_task)
    language_state = canonical_artifact_language_state(
        workspace,
        expected_author_task_id=author_task.id,
    )
    analysis_depth = workspace.analysis_depth_contract()
    if analysis_depth is None:
        raise ProcessingError("Review requires a persisted analysis-depth contract")
    verify_analysis_depth_contract(analysis_depth)
    analysis_depth_digest = stable_hash(
        analysis_depth.model_dump(mode="json"),
        length=64,
    )
    if (
        result.reviewed_snapshot_digest != plan.reviewed_snapshot_digest
        or result.reviewed_snapshot_digest != task.scope.get("reviewed_snapshot_digest")
        or result.target_build_id != plan.target.build_id
        or result.target_build_id != task.scope.get("target_build_id")
        or result.target_content_digest != plan.target.content_digest
        or result.target_content_digest != task.scope.get("target_content_digest")
        or task.scope.get("artifact_language_contract_digest") != language_state.contract_digest
        or task.scope.get("artifact_language_declaration_digest")
        != language_state.declaration_digest
        or task.scope.get("analysis_depth_contract_digest") != analysis_depth_digest
    ):
        raise ProcessingError("Review result targets a stale Skill snapshot")
    payload = packet.get("payload")
    if (
        not isinstance(payload, dict)
        or payload.get("artifact_language_contract")
        != language_state.contract.model_dump(mode="json")
        or payload.get("artifact_language_contract_digest") != language_state.contract_digest
        or payload.get("artifact_language_declaration")
        != language_state.declaration.model_dump(mode="json")
        or payload.get("artifact_language_declaration_digest") != language_state.declaration_digest
        or payload.get("analysis_depth_contract") != analysis_depth.model_dump(mode="json")
        or payload.get("analysis_depth_contract_digest") != analysis_depth_digest
    ):
        raise ProcessingError("Review packet has stale artifact-language state")
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
        curriculum_task = workspace.get_work_item(curriculum_record.producer_task_id)
        if curriculum_task.execution_context_id == task.execution_context_id:
            raise ProcessingError(
                "Review and curriculum Author require distinct engine-issued contexts"
            )
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
    critic_report = reconstruct_critic_report(task, result)
    workspace.write_json(review_path, critic_report)
    accepted, _canonical = workspace.accept_work_result(
        task_id=task.id,
        lease_token=result.lease_token,
        result_path=result_path,
        result_snapshot=result_snapshot,
        producer=reviewer,
        canonical_outputs=[
            workspace.canonical_output_file_snapshot(
                task.id,
                "critic-report",
                "default",
                review_path,
            ),
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
        critic_path = workspace.root / review_record.path
        if hash_file(critic_path) != review_record.digest:
            raise ProcessingError("Author repair critic report failed its digest check")
        report = CriticReport.model_validate_json(critic_path.read_text(encoding="utf-8"))
    except (OSError, PydanticValidationError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Invalid canonical critic report: {exc}") from exc
    if report.verdict != "fail":
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
    ensure_artifact_language_contract(workspace)
    language_state = canonical_artifact_language_state(
        workspace,
        expected_author_task_id=prior_author_task.id,
    )
    language_scope = {
        "artifact_language_contract_digest": language_state.contract_digest,
        "artifact_language_declaration_digest": language_state.declaration_digest,
    }
    language_packet: dict[str, object] = {
        "contract": language_state.contract.model_dump(mode="json"),
        "contract_digest": language_state.contract_digest,
        "declaration": language_state.declaration.model_dump(mode="json"),
        "declaration_digest": language_state.declaration_digest,
    }
    packet = {
        "instructions": (
            "Repair every blocking critic finding against the current canonical state. "
            "Write revised drafts only in this task's output directory and submit a complete "
            "Author result so publication remains atomic. Preserve unaffected grounded "
            "material and do not weaken capability claims merely to hide missing affordances. "
            "Reuse only verified visual asset candidates, keep indispensable images linked "
            "on demand, and remove decorative, illegible, private, or misleading selections. "
            "Preserve the pinned selected curriculum and do not reopen its user decision. "
            "Preserve the canonical artifact language exactly; do not translate or relabel the "
            "repair into another language."
        ),
        "prior_author_result": str(prior_author_task.result_path),
        "critic_report": {"path": str(review_record.path), "digest": review_record.digest},
        "repair_cycle": repair_cycle,
        "visual_asset_candidates": visual_asset_candidate_packet(workspace),
        "canonical_curriculum": curriculum_packet,
        "artifact_language": language_packet,
    }
    repair_scope: dict[str, object] = {
        "kind": "course-authoring-repair",
        "revision": repair_cycle + 1,
        "repair_of": prior_author_task.id,
        "review_task_id": failed_review_task.id,
        **curriculum_scope,
        **language_scope,
    }
    if isinstance(prior_author_task.scope.get("skill_id"), str):
        repair_scope["skill_id"] = str(prior_author_task.scope["skill_id"])
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope=repair_scope,
        persona_hint=AUTHOR_PERSONA,
        packet=packet,
        result_schema=AuthorResult.model_json_schema(mode="validation"),
        dependencies=[failed_review_task.id],
        snapshot_digest=run.snapshot_digest,
    )
