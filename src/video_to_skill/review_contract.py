"""Shared deterministic reconstruction for independent Review state and reports."""

from __future__ import annotations

import json

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.artifact_language import canonical_artifact_language_state
from video_to_skill.curriculum import (
    load_canonical_curriculum_checkpoint,
    validate_artifact_bound_curriculum,
    validate_curriculum_checkpoint_author_binding,
)
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import CurriculumDesign
from video_to_skill.orchestration import (
    ArtifactDraftSpec,
    BehaviorReport,
    CriticReport,
    ReviewResult,
)
from video_to_skill.utils import hash_file, stable_hash
from video_to_skill.visual_assets import canonical_visual_asset_candidates
from video_to_skill.work import WorkItem
from video_to_skill.workspace import Workspace

REVIEW_RECORD_KINDS = (
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


def verified_canonical_review_json(workspace: Workspace, kind: str) -> object:
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


def reconstruct_review_snapshot(
    workspace: Workspace,
    *,
    expected_author_task_id: str | None = None,
) -> tuple[str, dict[str, object]]:
    """Rehash every canonical input reviewed by both submission and compilation."""

    records: dict[str, dict[str, str]] = {}
    for kind in REVIEW_RECORD_KINDS:
        record = workspace.canonical_record(kind)
        if record is None:
            raise ProcessingError(f"Review requires canonical {kind}")
        verified_canonical_review_json(workspace, kind)
        records[kind] = {
            "path": str(record.path),
            "digest": record.digest,
            "producer_task_id": record.producer_task_id,
        }
    language_state = canonical_artifact_language_state(
        workspace,
        expected_author_task_id=expected_author_task_id,
    )
    records["artifact-language-contract"] = {
        "path": str(language_state.contract_path or ""),
        "digest": language_state.contract_digest,
        "producer_task_id": "",
    }
    language_declaration_record = workspace.canonical_record("artifact-language-declaration")
    records["artifact-language-declaration"] = {
        "path": str(language_state.declaration_path or ""),
        "digest": language_state.declaration_digest,
        "producer_task_id": (
            language_declaration_record.producer_task_id
            if language_declaration_record is not None
            else ""
        ),
    }
    checkpoint = load_canonical_curriculum_checkpoint(workspace)
    if checkpoint is not None:
        bound_author = validate_curriculum_checkpoint_author_binding(workspace, checkpoint)
        if expected_author_task_id is not None and bound_author.id != expected_author_task_id:
            raise ProcessingError("Review target is not the canonical checkpoint-bound Author")
        try:
            curriculum = CurriculumDesign.model_validate(
                verified_canonical_review_json(workspace, "curriculum")
            )
            artifact_values = verified_canonical_review_json(workspace, "artifact-plan")
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
        verified_canonical_review_json(workspace, "delivery-selection")
        records["delivery-selection"] = {
            "path": str(delivery_record.path),
            "digest": delivery_record.digest,
            "producer_task_id": delivery_record.producer_task_id,
        }
    artifacts = verified_canonical_review_json(workspace, "artifact-plan")
    if not isinstance(artifacts, list):
        raise ProcessingError("Canonical artifact plan must be a JSON list")
    draft_records: dict[str, dict[str, str]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("id"), str):
            raise ProcessingError("Canonical artifact plan contains an invalid artifact")
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


def reconstruct_review_reports(
    task: WorkItem,
    result: ReviewResult,
) -> tuple[CriticReport, BehaviorReport]:
    """Project the only canonical reports valid for one accepted ReviewResult."""

    critic = CriticReport(
        review_task_id=task.id,
        verdict=result.verdict,
        reviewed_snapshot_digest=result.reviewed_snapshot_digest,
        target_build_id=result.target_build_id,
        target_content_digest=result.target_content_digest,
        repair_cycle=int(task.scope["repair_cycle"]),
        findings=result.findings,
    )
    behavior = BehaviorReport(
        review_task_id=task.id,
        reviewed_snapshot_digest=result.reviewed_snapshot_digest,
        catalog_version=result.catalog_version,
        catalog_digest=result.catalog_digest,
        target_build_id=result.target_build_id,
        target_content_digest=result.target_content_digest,
        passed=all(check.passed is not False for check in result.behavior_checks),
        checks=result.behavior_checks,
    )
    return critic, behavior
