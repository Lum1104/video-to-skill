"""Deterministically compile, render, validate, and install canonical workspace state."""

from __future__ import annotations

import json
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from video_to_skill.analysis_depth import verify_analysis_depth_contract
from video_to_skill.artifact_language import (
    ArtifactLanguageDeclarationState,
    ArtifactLanguageResolution,
    canonical_artifact_language_state,
)
from video_to_skill.behavior import (
    BehaviorTargetSnapshot,
    lexical_path_without_symlinks,
    snapshot_behavior_target,
)
from video_to_skill.curriculum import (
    load_canonical_curriculum_checkpoint,
    validate_artifact_bound_curriculum,
    validate_curriculum_checkpoint_author_binding,
)
from video_to_skill.decompose import scoped_skill_candidate
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import (
    COURSE_SKILL_RENDERER_CONTRACT_VERSION,
    CapabilityProfile,
    CorePrinciple,
    CourseArtifact,
    CourseAsset,
    CourseCoverageLedger,
    CourseInteraction,
    CourseSkillBlueprint,
    CourseSkillClaim,
    CourseSkillEditionLineage,
    CourseSkillSource,
    CurriculumDesign,
    SemanticRelation,
    SemanticUnit,
    blueprint_seed_from_workspace,
    course_skill_build_id,
    render_course_skill_package,
    render_course_skill_review_target,
    validate_blueprint_against_workspace,
)
from video_to_skill.installation import (
    SkillHost,
    host_skill_root,
    install_generated_skill,
)
from video_to_skill.models import AnalysisDepthContract
from video_to_skill.orchestration import (
    ArtifactDraftSpec,
    CriticReport,
    DeliverySelection,
    InstructionalAffordance,
    ReviewResult,
)
from video_to_skill.review_contract import (
    reconstruct_critic_report,
    reconstruct_review_snapshot,
)
from video_to_skill.utils import hash_file, stable_hash
from video_to_skill.validation import render_validation_report, validate_skill
from video_to_skill.visual_assets import canonical_visual_asset_candidates
from video_to_skill.work import WorkRole, WorkState
from video_to_skill.workspace import Workspace


class CompileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompiledArtifactReference(CompileModel):
    id: str
    destination_path: str
    draft_path: Path
    draft_digest: str
    affordance_ids: list[str] = Field(default_factory=list)


class WorkspaceBuildReceipt(CompileModel):
    schema_version: str = "workspace-compiled-v1"
    build_id: str
    workspace_snapshot_digest: str
    name: str
    requested_output_language: str
    artifact_language: str
    artifact_language_resolution: ArtifactLanguageResolution
    artifact_language_declaration_state: ArtifactLanguageDeclarationState
    artifact_language_contract_digest: str
    artifact_language_declaration_digest: str
    analysis_depth_contract: AnalysisDepthContract
    semantic_map_digest: str
    curriculum_digest: str
    curriculum_options_digest: str | None = None
    selected_curriculum_digest: str | None = None
    instructional_affordance_digest: str
    assets_digest: str
    visual_asset_candidates_digest: str | None = None
    critic_report_digest: str
    delivery_selection_digest: str | None = None
    artifacts: list[CompiledArtifactReference]
    edition_lineage: CourseSkillEditionLineage | None = None

    @model_validator(mode="after")
    def coherent_curriculum_checkpoint(self) -> WorkspaceBuildReceipt:
        if (self.curriculum_options_digest is None) != (self.selected_curriculum_digest is None):
            raise ValueError("build receipt curriculum checkpoint digests must appear together")
        return self


class WorkspaceBuildResult(CompileModel):
    build_id: str
    name: str
    requested_output_language: str
    artifact_language: str
    artifact_language_resolution: ArtifactLanguageResolution
    artifact_language_declaration_state: ArtifactLanguageDeclarationState
    generated_path: Path
    installed_path: Path
    installation_status: str
    host: SkillHost
    scope: str
    course_coverage: str
    validation_report_path: Path


@dataclass(frozen=True)
class WorkspaceBlueprintProjection:
    """Canonical author state assembled without depending on a quality verdict."""

    blueprint: CourseSkillBlueprint
    requested_output_language: str
    artifact_language_resolution: ArtifactLanguageResolution
    artifact_language_declaration_state: ArtifactLanguageDeclarationState
    artifact_language_contract_digest: str
    artifact_language_declaration_digest: str
    artifact_references: list[CompiledArtifactReference]
    semantic_map_digest: str
    curriculum_digest: str
    curriculum_options_digest: str | None
    selected_curriculum_digest: str | None
    instructional_affordance_digest: str
    assets_digest: str
    visual_asset_candidates_digest: str | None
    delivery_selection_digest: str | None


def _canonical_json(workspace: Workspace, kind: str, record_id: str = "default") -> Any:
    record = workspace.canonical_record(kind, record_id)
    if record is None:
        raise ProcessingError(f"Compilation requires canonical {kind}:{record_id}")
    path = workspace.root / record.path
    if not path.is_file() or path.is_symlink() or hash_file(path) != record.digest:
        raise ProcessingError(f"Canonical record failed its digest check: {kind}:{record_id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Invalid canonical JSON for {kind}:{record_id}: {exc}") from exc


def _validated_list(model: type[BaseModel], values: Any, *, label: str) -> list[Any]:
    if not isinstance(values, list):
        raise ProcessingError(f"Canonical {label} must be a JSON list")
    try:
        return [model.model_validate(value) for value in values]
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid canonical {label}: {exc}") from exc


def _validate_selected_visual_assets(
    workspace: Workspace,
    assets: list[CourseAsset],
) -> None:
    candidates = {
        candidate.candidate_id: (candidate, image_path)
        for candidate, image_path in canonical_visual_asset_candidates(workspace)
    }
    for asset in assets:
        pair = candidates.get(asset.candidate_id)
        if pair is None:
            raise ProcessingError(
                f"Selected visual asset has no integrated Analyze candidate: {asset.candidate_id}"
            )
        candidate, image_path = pair
        expected_source_path = image_path.relative_to(workspace.root)
        if asset.source_path != expected_source_path:
            raise ProcessingError(
                f"Selected visual asset source does not match its canonical image: {asset.path}"
            )
        if asset.source_sha256 != candidate.sha256 or hash_file(image_path) != asset.source_sha256:
            raise ProcessingError(
                f"Selected visual asset failed its source digest check: {asset.path}"
            )
        if (
            asset.source_id != candidate.source_id
            or asset.evidence_ids != candidate.evidence_ids
            or asset.semantic_unit_ids != candidate.semantic_unit_ids
            or asset.presentation != candidate.presentation
            or asset.crop != candidate.crop
        ):
            raise ProcessingError(
                f"Selected visual asset derivation differs from Analyze: {asset.path}"
            )


def _fresh_canonical_target_snapshot(
    workspace: Workspace,
    projection: WorkspaceBlueprintProjection,
) -> BehaviorTargetSnapshot:
    target_root = workspace.analysis_dir / "review-targets"
    workspace.ensure_directory(target_root)
    fresh_path = target_root / f".compiler-fresh-{secrets.token_hex(16)}"
    try:
        render_course_skill_review_target(
            projection.blueprint,
            fresh_path,
            workspace_root=workspace.root,
            allowed_root=target_root,
        )
        return snapshot_behavior_target(
            fresh_path,
            expected_build_id=course_skill_build_id(projection.blueprint),
            allowed_root=target_root,
        )
    finally:
        if fresh_path.exists() or fresh_path.is_symlink():
            if fresh_path.is_dir() and not fresh_path.is_symlink():
                shutil.rmtree(fresh_path)
            else:
                fresh_path.unlink(missing_ok=True)


def _quality_gate(
    workspace: Workspace,
    projection: WorkspaceBlueprintProjection,
) -> str:
    raw_critic = _canonical_json(workspace, "critic-report")
    try:
        critic = CriticReport.model_validate(raw_critic)
    except PydanticValidationError as exc:
        raise ProcessingError(f"Canonical critic report is invalid: {exc}") from exc
    critic_record = workspace.canonical_record("critic-report")
    assert critic_record is not None
    review_task = workspace.get_work_item(critic_record.producer_task_id)
    if (
        review_task.role != WorkRole.REVIEW
        or review_task.scope.get("kind") != "independent-review"
        or review_task.state != WorkState.COMPLETE
        or critic.review_task_id != review_task.id
    ):
        raise ProcessingError("Canonical critic report is not bound to a completed Review")
    if review_task.result_path is None or review_task.result_digest is None:
        raise ProcessingError("Canonical Review has no accepted raw ReviewResult")
    accepted_result_path = workspace.root / review_task.result_path
    if (
        accepted_result_path.is_symlink()
        or not accepted_result_path.is_file()
        or hash_file(accepted_result_path) != review_task.result_digest
    ):
        raise ProcessingError("Accepted raw ReviewResult failed its digest check")
    try:
        accepted_result = ReviewResult.model_validate_json(
            accepted_result_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, PydanticValidationError) as exc:
        raise ProcessingError(f"Accepted raw ReviewResult is invalid: {exc}") from exc
    stored_reviewer = workspace.work_result_producer(review_task.id)
    if stored_reviewer != accepted_result.producer.model_dump(mode="json"):
        raise ProcessingError("Accepted ReviewResult producer differs from stored provenance")
    if (
        review_task.execution_context_id is None
        or accepted_result.execution_context_id != review_task.execution_context_id
    ):
        raise ProcessingError("Accepted ReviewResult has the wrong engine-issued context")
    expected_critic = reconstruct_critic_report(review_task, accepted_result)
    if raw_critic != expected_critic.model_dump(mode="json"):
        raise ProcessingError("Canonical critic report differs from the accepted ReviewResult")
    course_record = workspace.canonical_record("course")
    if (
        course_record is None
        or review_task.scope.get("author_task_id") != course_record.producer_task_id
    ):
        raise ProcessingError("Canonical Review targets a stale Author revision")
    current_review_snapshot, _canonical_packet = reconstruct_review_snapshot(
        workspace,
        expected_author_task_id=course_record.producer_task_id,
    )
    author_task = workspace.get_work_item(course_record.producer_task_id)
    author_producer = workspace.work_result_producer(author_task.id)
    if author_producer is None:
        raise ProcessingError("Canonical Review cannot identify the Author producer")
    same_author_name = stored_reviewer.get("name") == author_producer.get("name")
    same_author_run = stored_reviewer.get("run_id") is not None and stored_reviewer.get(
        "run_id"
    ) == author_producer.get("run_id")
    if (
        same_author_name
        or same_author_run
        or (
            author_task.execution_context_id is not None
            and author_task.execution_context_id == review_task.execution_context_id
        )
    ):
        raise ProcessingError("Independent Review provenance matches the Author")
    curriculum_record = workspace.canonical_record("curriculum-options")
    if curriculum_record is not None:
        curriculum_task = workspace.get_work_item(curriculum_record.producer_task_id)
        curriculum_producer = workspace.work_result_producer(curriculum_task.id)
        if curriculum_producer is None:
            raise ProcessingError("Canonical Review cannot identify the curriculum producer")
        same_curriculum_name = stored_reviewer.get("name") == curriculum_producer.get("name")
        same_curriculum_run = stored_reviewer.get("run_id") is not None and stored_reviewer.get(
            "run_id"
        ) == curriculum_producer.get("run_id")
        if (
            same_curriculum_name
            or same_curriculum_run
            or (
                curriculum_task.execution_context_id is not None
                and curriculum_task.execution_context_id == review_task.execution_context_id
            )
        ):
            raise ProcessingError("Independent Review provenance matches curriculum Author")

    packet_path = workspace.root / review_task.packet_path
    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Invalid canonical Review packet: {exc}") from exc
    if not isinstance(packet, dict) or stable_hash(packet, length=64) != review_task.packet_digest:
        raise ProcessingError("Canonical Review packet failed its digest check")
    payload = packet.get("payload")
    target_packet = payload.get("evaluation_target") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not isinstance(target_packet, dict):
        raise ProcessingError("Canonical Review packet has incomplete evaluation target")
    language_state = canonical_artifact_language_state(
        workspace,
        expected_author_task_id=course_record.producer_task_id,
    )
    if (
        review_task.scope.get("artifact_language_contract_digest") != language_state.contract_digest
        or review_task.scope.get("artifact_language_declaration_digest")
        != language_state.declaration_digest
        or payload.get("artifact_language_contract")
        != language_state.contract.model_dump(mode="json")
        or payload.get("artifact_language_contract_digest") != language_state.contract_digest
        or payload.get("artifact_language_declaration")
        != language_state.declaration.model_dump(mode="json")
        or payload.get("artifact_language_declaration_digest") != language_state.declaration_digest
    ):
        raise ProcessingError("Canonical Review targets stale artifact-language state")
    target_relative = Path(str(target_packet.get("path", "")))
    if target_relative.is_absolute() or ".." in target_relative.parts:
        raise ProcessingError("Canonical Review target path is unsafe")
    expected_build_id = course_skill_build_id(projection.blueprint)
    target = snapshot_behavior_target(
        workspace.root / target_relative,
        expected_build_id=expected_build_id,
        allowed_root=workspace.analysis_dir / "review-targets",
    )
    fresh_target = _fresh_canonical_target_snapshot(workspace, projection)
    if target.content_digest != fresh_target.content_digest or target.files != fresh_target.files:
        raise ProcessingError(
            "Evaluated review target differs from a fresh canonical engine render"
        )
    if (
        critic.target_build_id != expected_build_id
        or critic.target_content_digest != target.content_digest
        or review_task.scope.get("target_content_digest") != target.content_digest
        or target_packet.get("build_id") != target.build_id
        or target_packet.get("content_digest") != target.content_digest
        or accepted_result.reviewed_snapshot_digest != current_review_snapshot
        or review_task.scope.get("reviewed_snapshot_digest") != current_review_snapshot
        or payload.get("reviewed_snapshot_digest") != current_review_snapshot
        or payload.get("canonical") != _canonical_packet
        or critic.reviewed_snapshot_digest != current_review_snapshot
        or review_task.scope.get("renderer_contract_version")
        != COURSE_SKILL_RENDERER_CONTRACT_VERSION
        or target_packet.get("renderer_contract_version") != COURSE_SKILL_RENDERER_CONTRACT_VERSION
    ):
        raise ProcessingError("Canonical Review report targets a stale Skill preview")
    if critic.verdict != "pass":
        raise ProcessingError("Canonical independent Review has not passed")
    return critic_record.digest


def portable_build_matches(root: Path, expected_build_id: str) -> bool:
    try:
        candidate = lexical_path_without_symlinks(root, label="Portable Skill candidate")
    except ProcessingError:
        return False
    if not candidate.is_dir():
        return False
    manifest_path = candidate / "build-manifest.json"
    try:
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.stat().st_size > 4 * 1024 * 1024
        ):
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 2
            or manifest.get("build_id") != expected_build_id
        ):
            return False
        managed = manifest.get("managed_files")
        if not isinstance(managed, dict) or not managed:
            return False
        expected_paths = {"build-manifest.json"}
        for relative, entry in managed.items():
            if not isinstance(relative, str) or not isinstance(entry, dict):
                return False
            portable_path = PurePosixPath(relative)
            if portable_path.is_absolute() or ".." in portable_path.parts:
                return False
            managed_path = candidate.joinpath(*portable_path.parts)
            if (
                managed_path.is_symlink()
                or not managed_path.is_file()
                or hash_file(managed_path) != entry.get("sha256")
            ):
                return False
            expected_paths.add(portable_path.as_posix())
        actual_paths: set[str] = set()
        for path in candidate.rglob("*"):
            if path.is_symlink():
                return False
            if path.is_file():
                actual_paths.add(path.relative_to(candidate).as_posix())
        return actual_paths == expected_paths
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def assemble_workspace_blueprint(workspace: Workspace) -> WorkspaceBlueprintProjection:
    """Assemble the exact would-be portable Skill before independent evaluation."""

    semantic_units = _validated_list(
        SemanticUnit,
        _canonical_json(workspace, "semantic-map"),
        label="semantic map",
    )
    semantic_relations = _validated_list(
        SemanticRelation,
        _canonical_json(workspace, "semantic-relations"),
        label="semantic relations",
    )
    course_record_for_scope = workspace.canonical_record("course")
    if course_record_for_scope is not None:
        author_task_for_scope = workspace.get_work_item(course_record_for_scope.producer_task_id)
        scoped_skill_id = author_task_for_scope.scope.get("skill_id")
        if isinstance(scoped_skill_id, str):
            candidate = scoped_skill_candidate(workspace, scoped_skill_id)
            if candidate is not None:
                allowed_unit_ids = set(candidate.semantic_unit_ids)
                semantic_units = [unit for unit in semantic_units if unit.id in allowed_unit_ids]
                semantic_relations = [
                    rel
                    for rel in semantic_relations
                    if rel.from_unit_id in allowed_unit_ids and rel.to_unit_id in allowed_unit_ids
                ]
    artifacts = _validated_list(
        ArtifactDraftSpec,
        _canonical_json(workspace, "artifact-plan"),
        label="artifact plan",
    )
    try:
        curriculum = CurriculumDesign.model_validate(_canonical_json(workspace, "curriculum"))
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid canonical curriculum: {exc}") from exc
    checkpoint = load_canonical_curriculum_checkpoint(workspace)
    if checkpoint is not None:
        validate_curriculum_checkpoint_author_binding(workspace, checkpoint)
        validate_artifact_bound_curriculum(
            checkpoint,
            curriculum,
            artifacts,
            allow_localized_metadata=workspace.is_named_edition,
        )
    _validated_list(
        InstructionalAffordance,
        _canonical_json(workspace, "instructional-affordances"),
        label="instructional affordance ledger",
    )
    claims = _validated_list(
        CourseSkillClaim,
        _canonical_json(workspace, "claims"),
        label="claims",
    )
    assets = _validated_list(
        CourseAsset,
        _canonical_json(workspace, "assets"),
        label="assets",
    )
    _validate_selected_visual_assets(workspace, assets)
    course = _canonical_json(workspace, "course")
    course_record = workspace.canonical_record("course")
    if course_record is None:
        raise ProcessingError("Compilation requires canonical course")
    language_state = canonical_artifact_language_state(
        workspace,
        expected_author_task_id=course_record.producer_task_id,
    )
    delivery_record = workspace.canonical_record("delivery-selection")
    try:
        delivery_selection = (
            DeliverySelection.model_validate(_canonical_json(workspace, "delivery-selection"))
            if delivery_record is not None
            else None
        )
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid canonical delivery selection: {exc}") from exc
    seed = blueprint_seed_from_workspace(workspace)
    rendered_artifacts: list[CourseArtifact] = []
    artifact_references: list[CompiledArtifactReference] = []
    for artifact in artifacts:
        draft_record = workspace.canonical_record("artifact-draft", artifact.id)
        if draft_record is None:
            raise ProcessingError(f"Compilation requires artifact draft {artifact.id}")
        draft_path = workspace.root / draft_record.path
        if (
            not draft_path.is_file()
            or draft_path.is_symlink()
            or hash_file(draft_path) != draft_record.digest
            or draft_record.digest != artifact.draft_sha256
        ):
            raise ProcessingError(f"Artifact draft failed its digest check: {artifact.id}")
        try:
            content = draft_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProcessingError(f"Could not read artifact draft {artifact.id}: {exc}") from exc
        rendered_artifacts.append(
            CourseArtifact(
                id=artifact.id,
                path=artifact.path,
                title=artifact.title,
                modes=artifact.modes,
                disclosure=artifact.disclosure,
                use_when=artifact.use_when,
                independent_loading_reason=artifact.independent_loading_reason,
                semantic_unit_ids=artifact.semantic_unit_ids,
                topics=artifact.topics,
                content=content,
            )
        )
        artifact_references.append(
            CompiledArtifactReference(
                id=artifact.id,
                destination_path=artifact.path,
                draft_path=draft_record.path,
                draft_digest=draft_record.digest,
                affordance_ids=artifact.affordance_ids,
            )
        )
    raw_seed_limitations = seed["limitations"]
    if not isinstance(raw_seed_limitations, list) or not all(
        isinstance(item, str) for item in raw_seed_limitations
    ):
        raise ProcessingError("Workspace blueprint seed has invalid limitations")
    seed_limitations = list(raw_seed_limitations)
    raw_sources = seed["sources"]
    if not isinstance(raw_sources, list):
        raise ProcessingError("Workspace blueprint seed has invalid sources")
    course_limitations = list(course.get("limitations", []))
    try:
        from video_to_skill.editions import public_edition_lineage

        raw_edition_lineage = public_edition_lineage(workspace)
        blueprint = CourseSkillBlueprint(
            name=(delivery_selection.name if delivery_selection is not None else course["name"]),
            title=course["title"],
            description=course["description"],
            scope=course["scope"],
            artifact_language=course["artifact_language"],
            interaction=CourseInteraction.model_validate(_canonical_json(workspace, "interaction")),
            capability_profile=CapabilityProfile.model_validate(
                _canonical_json(workspace, "capability-profile")
            ),
            curriculum=curriculum,
            prerequisites=course.get("prerequisites", []),
            core_principles=[
                CorePrinciple.model_validate(item) for item in course.get("core_principles", [])
            ],
            semantic_units=semantic_units,
            semantic_relations=semantic_relations,
            artifacts=rendered_artifacts,
            assets=assets,
            sources=[CourseSkillSource.model_validate(item) for item in raw_sources],
            coverage_ledger=CourseCoverageLedger.model_validate(seed["coverage_ledger"]),
            claims=claims,
            limitations=list(dict.fromkeys([*seed_limitations, *course_limitations])),
            edition_lineage=(
                CourseSkillEditionLineage.model_validate(raw_edition_lineage)
                if raw_edition_lineage is not None
                else None
            ),
        )
    except (KeyError, TypeError, PydanticValidationError, ValueError) as exc:
        raise ProcessingError(f"Canonical workspace state cannot compile a Skill: {exc}") from exc
    validate_blueprint_against_workspace(blueprint, workspace)
    semantic_record = workspace.canonical_record("semantic-map")
    curriculum_record = workspace.canonical_record("curriculum")
    affordance_record = workspace.canonical_record("instructional-affordances")
    assets_record = workspace.canonical_record("assets")
    visual_candidates_record = workspace.canonical_record("visual-asset-candidates")
    assert semantic_record is not None
    assert curriculum_record is not None
    assert affordance_record is not None
    assert assets_record is not None
    return WorkspaceBlueprintProjection(
        blueprint=blueprint,
        requested_output_language=language_state.contract.requested_output_language,
        artifact_language_resolution=language_state.contract.resolution,
        artifact_language_declaration_state=language_state.declaration.declaration_state,
        artifact_language_contract_digest=language_state.contract_digest,
        artifact_language_declaration_digest=language_state.declaration_digest,
        artifact_references=artifact_references,
        semantic_map_digest=semantic_record.digest,
        curriculum_digest=curriculum_record.digest,
        curriculum_options_digest=(
            checkpoint.plan_record.digest if checkpoint is not None else None
        ),
        selected_curriculum_digest=(
            checkpoint.selection_record.digest if checkpoint is not None else None
        ),
        instructional_affordance_digest=affordance_record.digest,
        assets_digest=assets_record.digest,
        visual_asset_candidates_digest=(
            visual_candidates_record.digest if visual_candidates_record is not None else None
        ),
        delivery_selection_digest=(delivery_record.digest if delivery_record is not None else None),
    )


def compile_workspace_blueprint(
    workspace: Workspace,
) -> tuple[CourseSkillBlueprint, WorkspaceBuildReceipt, Path]:
    analysis_depth = workspace.analysis_depth_contract()
    if analysis_depth is None:
        raise ProcessingError("Compilation requires a persisted analysis-depth contract")
    verify_analysis_depth_contract(analysis_depth)
    semantic_record = workspace.canonical_record("semantic-map")
    if semantic_record is not None:
        producer = workspace.get_work_item(semantic_record.producer_task_id)
        if producer.role == WorkRole.ANALYZE:
            packet_path = workspace.root / producer.packet_path
            if (
                not packet_path.is_file()
                or packet_path.is_symlink()
                or packet_path.stat().st_size > 8 * 1024 * 1024
            ):
                raise ProcessingError("Canonical Analyze packet failed its digest check")
            try:
                packet = json.loads(packet_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProcessingError(f"Invalid canonical Analyze packet: {exc}") from exc
            if stable_hash(packet, length=64) != producer.packet_digest:
                raise ProcessingError("Canonical Analyze packet failed its digest check")
            if not isinstance(packet, dict):
                raise ProcessingError("Canonical Analyze packet must be a JSON object")
            payload = packet.get("payload")
            if not isinstance(payload, dict) or payload.get(
                "analysis_depth"
            ) != analysis_depth.model_dump(mode="json"):
                raise ProcessingError(
                    "Canonical Analyze result is not bound to the persisted analysis-depth contract"
                )
    projection = assemble_workspace_blueprint(workspace)
    critic_digest = _quality_gate(workspace, projection)
    blueprint = projection.blueprint
    build_id = course_skill_build_id(blueprint)
    receipt = WorkspaceBuildReceipt(
        build_id=build_id,
        workspace_snapshot_digest=workspace.workspace_snapshot_digest(),
        name=blueprint.name,
        requested_output_language=projection.requested_output_language,
        artifact_language=blueprint.artifact_language,
        artifact_language_resolution=projection.artifact_language_resolution,
        artifact_language_declaration_state=projection.artifact_language_declaration_state,
        artifact_language_contract_digest=projection.artifact_language_contract_digest,
        artifact_language_declaration_digest=projection.artifact_language_declaration_digest,
        analysis_depth_contract=analysis_depth,
        semantic_map_digest=projection.semantic_map_digest,
        curriculum_digest=projection.curriculum_digest,
        curriculum_options_digest=projection.curriculum_options_digest,
        selected_curriculum_digest=projection.selected_curriculum_digest,
        instructional_affordance_digest=projection.instructional_affordance_digest,
        assets_digest=projection.assets_digest,
        visual_asset_candidates_digest=projection.visual_asset_candidates_digest,
        critic_report_digest=critic_digest,
        delivery_selection_digest=projection.delivery_selection_digest,
        artifacts=projection.artifact_references,
        edition_lineage=blueprint.edition_lineage,
    )
    build_directory = workspace.builds_dir / build_id
    workspace.write_json(build_directory / "blueprint.json", receipt)
    workspace.write_json(
        build_directory / "critic-report.json",
        _canonical_json(workspace, "critic-report"),
    )
    return blueprint, receipt, build_directory


def build_workspace_skill(
    workspace: Workspace,
    *,
    host: SkillHost,
    output: Path,
    project: bool = False,
    project_root: Path | None = None,
    skill_root: Path | None = None,
    run_official_validation: bool = True,
) -> WorkspaceBuildResult:
    blueprint, receipt, build_directory = compile_workspace_blueprint(workspace)
    generated = lexical_path_without_symlinks(output, label="Generated Skill output")
    if generated.exists():
        try:
            manifest = json.loads((generated / "build-manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProcessingError(
                f"Existing generated output is not this resumable build: {generated}"
            ) from exc
        if manifest.get("build_id") != receipt.build_id:
            raise ProcessingError(
                f"Existing generated output belongs to a different build: {generated}"
            )
    else:
        generated = render_course_skill_package(
            blueprint,
            generated,
            workspace_root=workspace.root,
        )
    critic_report = CriticReport.model_validate(_canonical_json(workspace, "critic-report"))
    generated_snapshot = snapshot_behavior_target(
        generated,
        expected_build_id=receipt.build_id,
    )
    if generated_snapshot.content_digest != critic_report.target_content_digest:
        raise ProcessingError("Rendered Skill bytes differ from the independently reviewed target")
    report = validate_skill(
        generated,
        run_official=run_official_validation,
        check_code=True,
    )
    validation_path = build_directory / "validation-report.json"
    workspace.write_json(validation_path, report)
    if not report.valid:
        raise ProcessingError(render_validation_report(report))
    root = skill_root or host_skill_root(
        host,
        project=project,
        project_root=project_root,
    )
    installed, status = install_generated_skill(generated, root)
    return WorkspaceBuildResult(
        build_id=receipt.build_id,
        name=blueprint.name,
        requested_output_language=receipt.requested_output_language,
        artifact_language=receipt.artifact_language,
        artifact_language_resolution=receipt.artifact_language_resolution,
        artifact_language_declaration_state=receipt.artifact_language_declaration_state,
        generated_path=generated,
        installed_path=installed,
        installation_status=status,
        host=host,
        scope="project" if project else "user",
        course_coverage=(
            blueprint.coverage_ledger.state if blueprint.coverage_ledger is not None else "unproven"
        ),
        validation_report_path=validation_path,
    )
