"""Independent Review task planning, acceptance, and repair planning."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.analysis_depth import verify_analysis_depth_contract
from video_to_skill.artifact_language import (
    canonical_artifact_language_state,
    ensure_artifact_language_contract,
)
from video_to_skill.author import AUTHOR_PERSONA
from video_to_skill.behavior import (
    BEHAVIOR_CATALOG_VERSION,
    BEHAVIOR_CONTRACT_VERSION,
    BehaviorTargetSnapshot,
    behavior_catalog_digest,
    build_behavior_catalog,
    check_matches_scenario,
    snapshot_behavior_target,
)
from video_to_skill.compiler import assemble_workspace_blueprint
from video_to_skill.curriculum import (
    load_canonical_curriculum_checkpoint,
    load_canonical_curriculum_plan,
    load_canonical_curriculum_selection,
    validate_artifact_bound_curriculum,
    validate_curriculum_checkpoint_author_binding,
)
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import (
    COURSE_SKILL_RENDERER_CONTRACT_VERSION,
    CurriculumDesign,
    course_skill_build_id,
    render_course_skill_review_target,
)
from video_to_skill.models import EvidenceGapType
from video_to_skill.orchestration import (
    ArtifactDraftSpec,
    AuthorResult,
    BehaviorReport,
    BehaviorScenario,
    BehaviorTrialResult,
    CriticReport,
    InstructionalAffordance,
    ReviewResult,
)
from video_to_skill.review_contract import (
    reconstruct_review_reports,
    reconstruct_review_snapshot,
    verified_canonical_review_json,
)
from video_to_skill.utils import hash_file, stable_hash
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

BEHAVIOR_TRIAL_PERSONA = (
    "You are a clean-room Agent Skill behavior trial runner. In a fresh context, activate only "
    "the supplied immutable generated Skill preview, execute the single supplied user prompt, "
    "and record the exact bounded exchange and file or side-effect trace without grading it."
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


@dataclass(frozen=True)
class BehaviorEvaluationPlan:
    reviewed_snapshot_digest: str
    canonical_packet: dict[str, object]
    scenarios: list[BehaviorScenario]
    catalog_digest: str
    target: BehaviorTargetSnapshot


def _verified_canonical_json(workspace: Workspace, kind: str) -> object:
    return verified_canonical_review_json(workspace, kind)

    # Kept unreachable for source-compatible tracebacks in legacy workspaces.
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
    return reconstruct_review_snapshot(
        workspace,
        expected_author_task_id=expected_author_task_id,
    )

    # Kept unreachable for source-compatible tracebacks in legacy workspaces.
    records: dict[str, dict[str, str]] = {}
    for kind in _REVIEW_RECORD_KINDS:
        record = workspace.canonical_record(kind)
        if record is None:
            raise ProcessingError(f"Review requires canonical {kind}")
        _verified_canonical_json(workspace, kind)
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
        validate_artifact_bound_curriculum(
            checkpoint,
            curriculum,
            artifact_specs,
            allow_localized_metadata=workspace.edition_id is not None,
        )
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
    delivery_record = workspace.canonical_record("delivery-selection")
    if delivery_record is not None:
        _verified_canonical_json(workspace, "delivery-selection")
        records["delivery-selection"] = {
            "path": str(delivery_record.path),
            "digest": delivery_record.digest,
            "producer_task_id": delivery_record.producer_task_id,
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
        draft_path = workspace.root / record.path
        try:
            if (
                draft_path.is_symlink()
                or not draft_path.is_file()
                or draft_path.stat().st_size > 512 * 1024
                or hash_file(draft_path) != record.digest
            ):
                raise ProcessingError(
                    f"Canonical Review draft failed its digest check: {artifact_id}"
                )
        except OSError as exc:
            raise ProcessingError(f"Could not verify canonical Review draft: {exc}") from exc
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


def _verified_task_packet(workspace: Workspace, task: WorkItem) -> dict[str, object]:
    try:
        packet = json.loads((workspace.root / task.packet_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Invalid Review task packet: {exc}") from exc
    if not isinstance(packet, dict) or stable_hash(packet, length=64) != task.packet_digest:
        raise ProcessingError("Review task packet failed its digest check")
    return packet


def _behavior_evaluation_plan(
    workspace: Workspace,
    *,
    author_task: WorkItem,
) -> BehaviorEvaluationPlan:
    reviewed_snapshot, canonical_packet = _review_snapshot(
        workspace,
        expected_author_task_id=author_task.id,
    )
    projection = assemble_workspace_blueprint(workspace)
    raw_affordances = _verified_canonical_json(workspace, "instructional-affordances")
    if not isinstance(raw_affordances, list):
        raise ProcessingError("Canonical instructional affordances must be a JSON list")
    try:
        affordances = [InstructionalAffordance.model_validate(item) for item in raw_affordances]
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid canonical instructional affordances: {exc}") from exc
    scenarios = build_behavior_catalog(projection.blueprint.semantic_units, affordances)
    catalog_digest = behavior_catalog_digest(scenarios)
    build_id = course_skill_build_id(projection.blueprint)
    target_key = stable_hash(
        {
            "contract": BEHAVIOR_CONTRACT_VERSION,
            "renderer_contract": COURSE_SKILL_RENDERER_CONTRACT_VERSION,
            "catalog": catalog_digest,
            "reviewed_snapshot": reviewed_snapshot,
            "build_id": build_id,
        },
        length=32,
    )
    target_path = workspace.analysis_dir / "behavior-targets" / target_key
    target_root = workspace.analysis_dir / "behavior-targets"
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
            raise ProcessingError("Cached behavior target cannot be a symlink")
        if target_path.exists():
            existing = snapshot_behavior_target(
                target_path,
                expected_build_id=build_id,
                allowed_root=target_root,
            )
            if existing.content_digest != fresh.content_digest or existing.files != fresh.files:
                raise ProcessingError(
                    "Cached behavior target differs from a fresh canonical engine render"
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
    return BehaviorEvaluationPlan(
        reviewed_snapshot_digest=reviewed_snapshot,
        canonical_packet=canonical_packet,
        scenarios=scenarios,
        catalog_digest=catalog_digest,
        target=target,
    )


def plan_behavior_trial_tasks(
    workspace: Workspace,
    run: AnalysisRun,
    *,
    author_task: WorkItem,
    repair_cycle: int = 0,
) -> list[WorkItem]:
    """Create one prompt-isolated host dispatch per applicable catalog scenario."""

    if (
        author_task.role != WorkRole.AUTHOR
        or author_task.scope.get("kind") not in {"course-authoring", "course-authoring-repair"}
        or author_task.state != WorkState.COMPLETE
    ):
        raise ProcessingError("Behavior trials require a completed Author task")
    if repair_cycle < 0 or repair_cycle > MAX_REPAIR_CYCLES:
        raise ProcessingError("Behavior trial repair cycle is outside the supported range")
    plan = _behavior_evaluation_plan(workspace, author_task=author_task)
    language_state = canonical_artifact_language_state(
        workspace,
        expected_author_task_id=author_task.id,
    )
    target_path = plan.target.path.relative_to(workspace.root)
    tasks: list[WorkItem] = []
    for scenario in plan.scenarios:
        if scenario.applicability != "required":
            continue
        tasks.append(
            workspace.ensure_work_item(
                run_id=run.id,
                role=WorkRole.REVIEW,
                scope={
                    "kind": "behavior-trial",
                    "contract_version": BEHAVIOR_CONTRACT_VERSION,
                    "renderer_contract_version": COURSE_SKILL_RENDERER_CONTRACT_VERSION,
                    "catalog_version": BEHAVIOR_CATALOG_VERSION,
                    "catalog_digest": plan.catalog_digest,
                    "scenario_id": scenario.id,
                    "scenario_digest": scenario.scenario_digest,
                    "author_task_id": author_task.id,
                    "reviewed_snapshot_digest": plan.reviewed_snapshot_digest,
                    "target_build_id": plan.target.build_id,
                    "target_content_digest": plan.target.content_digest,
                    "repair_cycle": repair_cycle,
                    "artifact_language_contract_digest": language_state.contract_digest,
                    "artifact_language_declaration_digest": language_state.declaration_digest,
                },
                persona_hint=BEHAVIOR_TRIAL_PERSONA,
                packet={
                    "instructions": (
                        "Use a new context containing only this packet and the immutable Skill "
                        "target. Activate the Skill, execute exactly one user turn, and record "
                        "the assistant turn plus every generated-Skill file access and side "
                        "effect. Do not grade, repair, or summarize the behavior. Submit directly."
                    ),
                    "evaluation_target": {
                        "path": str(target_path),
                        "build_id": plan.target.build_id,
                        "content_digest": plan.target.content_digest,
                        "renderer_contract_version": COURSE_SKILL_RENDERER_CONTRACT_VERSION,
                    },
                    "scenario": {"id": scenario.id, "prompt": scenario.prompt},
                },
                result_schema=BehaviorTrialResult.model_json_schema(mode="validation"),
                dependencies=[author_task.id],
                snapshot_digest=run.snapshot_digest,
            )
        )
    return tasks


def _load_behavior_trial_result(payload: bytes) -> BehaviorTrialResult:
    try:
        return BehaviorTrialResult.model_validate_json(payload)
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid behavior trial result: {exc}") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Could not read behavior trial result: {exc}") from exc


def _verify_trial_against_task(
    workspace: Workspace,
    task: WorkItem,
    result: BehaviorTrialResult,
) -> BehaviorTargetSnapshot:
    packet = _verified_task_packet(workspace, task)
    payload = packet.get("payload")
    if not isinstance(payload, dict):
        raise ProcessingError("Behavior trial packet has no payload")
    target_packet = payload.get("evaluation_target")
    scenario_packet = payload.get("scenario")
    if not isinstance(target_packet, dict) or not isinstance(scenario_packet, dict):
        raise ProcessingError("Behavior trial packet is incomplete")
    if (
        result.task_id != task.id
        or result.snapshot_digest != task.snapshot_digest
        or result.execution_context_id != task.execution_context_id
        or result.scenario_id != task.scope.get("scenario_id")
        or result.scenario_digest != task.scope.get("scenario_digest")
        or result.target_content_digest != task.scope.get("target_content_digest")
        or result.scenario_id != scenario_packet.get("id")
        or result.turns[0].content != scenario_packet.get("prompt")
    ):
        raise ProcessingError("Behavior trial result does not belong to this task contract")
    target_relative = Path(str(target_packet.get("path", "")))
    if target_relative.is_absolute() or ".." in target_relative.parts:
        raise ProcessingError("Behavior trial target path is unsafe")
    target = snapshot_behavior_target(
        workspace.root / target_relative,
        expected_build_id=str(task.scope["target_build_id"]),
        allowed_root=workspace.analysis_dir / "behavior-targets",
    )
    if target.content_digest != result.target_content_digest:
        raise ProcessingError("Behavior trial target changed after dispatch")
    for access in result.artifact_accesses:
        expected = target.files.get(access.path)
        if expected is None or access.sha256 != expected.get("sha256"):
            raise ProcessingError(
                f"Behavior trial references an unknown or changed Skill file: {access.path}"
            )
    return target


def submit_behavior_trial_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> WorkItem:
    task = workspace.get_work_item(task_id)
    if task.role != WorkRole.REVIEW or task.scope.get("kind") != "behavior-trial":
        raise ProcessingError(f"Task is not a behavior trial: {task_id}")
    result_snapshot = workspace.task_output_file_snapshot(
        task_id,
        result_path,
        max_bytes=2 * 1024 * 1024,
    )
    result = _load_behavior_trial_result(result_snapshot.payload)
    _verify_trial_against_task(workspace, task, result)
    accepted, _records = workspace.accept_work_result(
        task_id=task.id,
        lease_token=result.lease_token,
        result_path=result_path,
        result_snapshot=result_snapshot,
        producer=result.producer.model_dump(mode="json"),
    )
    return accepted


def _verified_completed_trials(
    workspace: Workspace,
    tasks: list[WorkItem],
    *,
    plan: BehaviorEvaluationPlan,
) -> dict[str, tuple[WorkItem, BehaviorTrialResult]]:
    expected = {
        scenario.id: scenario for scenario in plan.scenarios if scenario.applicability == "required"
    }
    if len(tasks) != len(expected):
        raise ProcessingError("Behavior trial set is incomplete")
    results: dict[str, tuple[WorkItem, BehaviorTrialResult]] = {}
    execution_context_ids: set[str] = set()
    for task in tasks:
        if (
            task.state != WorkState.COMPLETE
            or task.result_path is None
            or task.result_digest is None
        ):
            raise ProcessingError("Behavior Review requires completed fresh-context trials")
        result_path = workspace.root / task.result_path
        result_payload = workspace.read_file_bytes(result_path, max_bytes=2 * 1024 * 1024)
        if sha256(result_payload).hexdigest() != task.result_digest:
            raise ProcessingError("Accepted behavior trial result failed its digest check")
        result = _load_behavior_trial_result(result_payload)
        _verify_trial_against_task(workspace, task, result)
        scenario = expected.get(result.scenario_id)
        if scenario is None or result.scenario_digest != scenario.scenario_digest:
            raise ProcessingError("Behavior trial does not match the current catalog")
        if result.scenario_id in results:
            raise ProcessingError("Behavior scenario was executed more than once")
        if result.execution_context_id in execution_context_ids:
            raise ProcessingError("Behavior trials must use distinct engine-issued contexts")
        execution_context_ids.add(result.execution_context_id)
        results[result.scenario_id] = (task, result)
    if set(results) != set(expected):
        raise ProcessingError("Behavior trial set does not cover every applicable scenario")
    return results


def plan_review_task(
    workspace: Workspace,
    run: AnalysisRun,
    *,
    author_task: WorkItem,
    behavior_trial_tasks: list[WorkItem],
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
    plan = _behavior_evaluation_plan(workspace, author_task=author_task)
    trials = _verified_completed_trials(
        workspace,
        behavior_trial_tasks,
        plan=plan,
    )
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
            "Independently audit source-meaning retention and instructional-affordance "
            "coverage, then grounding, disclosure, runtime behavior, safety, and scope. "
            "Inspect the immutable rendered Skill target and canonical files directly; judge "
            "every catalog scenario from its separate raw fresh-context trial. Treat missing "
            "quick reference, operational "
            "validation or recovery, capstone scoring, progressive hints, retry, and teaching "
            "scaffolds as product losses when the claimed capability requires them. Inspect "
            "every selected teaching visual and audit necessity, legibility, surrounding "
            "context, privacy, evidence grounding, and whether artifacts load it only on demand. "
            f"The canonical artifact language is {language_state.declaration.artifact_language!r}; "
            "audit all authored prose, titles, welcome text, and metadata for consistency with "
            "that declaration. Do not infer consistency merely from the declared field. Runtime "
            "interaction must still follow the learner's language by default."
        ),
        "artifact_language_contract": language_state.contract.model_dump(mode="json"),
        "artifact_language_contract_digest": language_state.contract_digest,
        "artifact_language_declaration": language_state.declaration.model_dump(mode="json"),
        "artifact_language_declaration_digest": language_state.declaration_digest,
        "analysis_depth_contract": analysis_depth.model_dump(mode="json"),
        "analysis_depth_contract_digest": analysis_depth_digest,
        "reviewed_snapshot_digest": plan.reviewed_snapshot_digest,
        "canonical": plan.canonical_packet,
        "behavior_catalog": {
            "version": BEHAVIOR_CATALOG_VERSION,
            "digest": plan.catalog_digest,
            "scenarios": [scenario.model_dump(mode="json") for scenario in plan.scenarios],
        },
        "evaluation_target": {
            "path": str(plan.target.path.relative_to(workspace.root)),
            "build_id": plan.target.build_id,
            "content_digest": plan.target.content_digest,
            "renderer_contract_version": COURSE_SKILL_RENDERER_CONTRACT_VERSION,
        },
        "behavior_trials": [
            {
                "scenario_id": scenario_id,
                "task_id": task.id,
                "result_path": str(task.result_path),
                "result_digest": str(task.result_digest),
            }
            for scenario_id, (task, _result) in trials.items()
        ],
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
            "contract_version": BEHAVIOR_CONTRACT_VERSION,
            "renderer_contract_version": COURSE_SKILL_RENDERER_CONTRACT_VERSION,
            "catalog_version": BEHAVIOR_CATALOG_VERSION,
            "catalog_digest": plan.catalog_digest,
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
            *(task.id for task in behavior_trial_tasks),
            *(additional_dependencies or []),
        ],
        snapshot_digest=run.snapshot_digest,
    )


def _load_review_result(payload: bytes) -> ReviewResult:
    try:
        raw = payload.decode("utf-8")
        try:
            version = json.loads(raw).get("schema_version")
        except (json.JSONDecodeError, AttributeError):
            version = None
        if version == 1:
            raise ProcessingError(
                "Legacy Review schema 1 cannot satisfy behavior catalog v2; dispatch fresh "
                "behavior trials and an independent Review"
            )
        return ReviewResult.model_validate_json(raw)
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
    _verified_task_packet(workspace, task)
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
    plan = _behavior_evaluation_plan(
        workspace,
        author_task=author_task,
    )
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
        or result.catalog_version != BEHAVIOR_CATALOG_VERSION
        or result.catalog_version != task.scope.get("catalog_version")
        or result.catalog_digest != plan.catalog_digest
        or result.catalog_digest != task.scope.get("catalog_digest")
        or result.target_build_id != plan.target.build_id
        or result.target_build_id != task.scope.get("target_build_id")
        or result.target_content_digest != plan.target.content_digest
        or result.target_content_digest != task.scope.get("target_content_digest")
        or task.scope.get("artifact_language_contract_digest") != language_state.contract_digest
        or task.scope.get("artifact_language_declaration_digest")
        != language_state.declaration_digest
        or task.scope.get("analysis_depth_contract_digest") != analysis_depth_digest
    ):
        raise ProcessingError("Review result targets a stale behavior catalog or Skill snapshot")
    packet = _verified_task_packet(workspace, task)
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
    trial_entries = payload.get("behavior_trials") if isinstance(payload, dict) else None
    if not isinstance(trial_entries, list):
        raise ProcessingError("Review packet has no behavior trial evidence")
    trial_tasks: list[WorkItem] = []
    for entry in trial_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("task_id"), str):
            raise ProcessingError("Review packet has an invalid behavior trial reference")
        trial_tasks.append(workspace.get_work_item(str(entry["task_id"])))
    trials = _verified_completed_trials(workspace, trial_tasks, plan=plan)
    if len(result.behavior_checks) != len(plan.scenarios):
        raise ProcessingError("Review must account for every catalog scenario exactly once")
    for check, scenario in zip(result.behavior_checks, plan.scenarios, strict=True):
        if not check_matches_scenario(check, scenario):
            raise ProcessingError(f"Review changed catalog scenario {scenario.id}")
        trial_pair = trials.get(scenario.id)
        if scenario.applicability == "required":
            if trial_pair is None:
                raise ProcessingError(f"Review lacks trial evidence for {scenario.id}")
            trial_task, trial_result = trial_pair
            if (
                check.trial_task_id != trial_task.id
                or check.trial_result_digest != trial_task.result_digest
                or check.target_content_digest != plan.target.content_digest
                or any(index >= len(trial_result.turns) for index in check.evidence_turn_indices)
            ):
                raise ProcessingError(f"Review cited stale trial evidence for {scenario.id}")
        elif trial_pair is not None:
            raise ProcessingError(
                f"Not-applicable scenario unexpectedly has a trial: {scenario.id}"
            )
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
    for _scenario_id, (trial_task, _trial_result) in trials.items():
        trial_producer = workspace.work_result_producer(trial_task.id)
        if trial_producer is None:
            raise ProcessingError("Review cannot identify a behavior trial producer")
        if reviewer.get("name") == trial_producer.get("name") or reviewer.get(
            "run_id"
        ) == trial_producer.get("run_id"):
            raise ProcessingError("Review producer must be independent of behavior trial producers")
        if trial_task.execution_context_id == task.execution_context_id:
            raise ProcessingError(
                "Review and behavior trials require distinct engine-issued contexts"
            )
    output = workspace.tasks_dir / task.id / "output"
    review_path = output / "critic-report.json"
    behavior_path = output / "behavior-report.json"
    critic_report, behavior_report = reconstruct_review_reports(task, result)
    workspace.write_json(review_path, critic_report)
    workspace.write_json(behavior_path, behavior_report)
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
            workspace.canonical_output_file_snapshot(
                task.id,
                "behavior-report",
                "default",
                behavior_path,
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
    behavior_record = workspace.canonical_record("behavior-report")
    if behavior_record is None or behavior_record.producer_task_id != failed_review_task.id:
        raise ProcessingError("Author repair requires the failed canonical behavior report")
    try:
        critic_path = workspace.root / review_record.path
        behavior_path = workspace.root / behavior_record.path
        if (
            hash_file(critic_path) != review_record.digest
            or hash_file(behavior_path) != behavior_record.digest
        ):
            raise ProcessingError("Author repair Review reports failed their digest checks")
        report = CriticReport.model_validate_json(critic_path.read_text(encoding="utf-8"))
        behavior_report = BehaviorReport.model_validate_json(
            behavior_path.read_text(encoding="utf-8")
        )
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
        "behavior_report": {
            "path": str(behavior_record.path),
            "digest": behavior_record.digest,
        },
        "behavior_trials": [
            {
                "scenario_id": check.id,
                "task_id": check.trial_task_id,
                "result_digest": check.trial_result_digest,
            }
            for check in behavior_report.checks
            if check.trial_task_id is not None
        ],
        "repair_cycle": repair_cycle,
        "visual_asset_candidates": visual_asset_candidate_packet(workspace),
        "canonical_curriculum": curriculum_packet,
        "artifact_language": language_packet,
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
            **language_scope,
        },
        persona_hint=AUTHOR_PERSONA,
        packet=packet,
        result_schema=AuthorResult.model_json_schema(mode="validation"),
        dependencies=[failed_review_task.id],
        snapshot_digest=run.snapshot_digest,
    )
