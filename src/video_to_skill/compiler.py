"""Deterministically compile, render, validate, and install canonical workspace state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from video_to_skill.errors import ProcessingError
from video_to_skill.generation import (
    CapabilityProfile,
    CorePrinciple,
    CourseArtifact,
    CourseAsset,
    CourseCoverageLedger,
    CourseInteraction,
    CourseSkillBlueprint,
    CourseSkillClaim,
    CourseSkillSource,
    CurriculumDesign,
    SemanticRelation,
    SemanticUnit,
    blueprint_seed_from_workspace,
    course_skill_build_id,
    render_course_skill_package,
    validate_blueprint_against_workspace,
)
from video_to_skill.installation import (
    SkillHost,
    host_skill_root,
    install_generated_skill,
)
from video_to_skill.orchestration import (
    ArtifactDraftSpec,
    InstructionalAffordance,
)
from video_to_skill.utils import atomic_write_json, hash_file
from video_to_skill.validation import render_validation_report, validate_skill
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
    semantic_map_digest: str
    curriculum_digest: str
    instructional_affordance_digest: str
    critic_report_digest: str
    behavior_report_digest: str
    artifacts: list[CompiledArtifactReference]


class WorkspaceBuildResult(CompileModel):
    build_id: str
    name: str
    generated_path: Path
    installed_path: Path
    installation_status: str
    host: SkillHost
    scope: str
    course_coverage: str
    validation_report_path: Path


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


def _quality_gate(workspace: Workspace) -> tuple[str, str]:
    critic = _canonical_json(workspace, "critic-report")
    behavior = _canonical_json(workspace, "behavior-report")
    if critic.get("verdict") != "pass":
        raise ProcessingError("Canonical independent Review has not passed")
    if behavior.get("passed") is not True:
        raise ProcessingError("Canonical behavior Review has not passed")
    critic_record = workspace.canonical_record("critic-report")
    behavior_record = workspace.canonical_record("behavior-report")
    assert critic_record is not None and behavior_record is not None
    return critic_record.digest, behavior_record.digest


def compile_workspace_blueprint(
    workspace: Workspace,
) -> tuple[CourseSkillBlueprint, WorkspaceBuildReceipt, Path]:
    critic_digest, behavior_digest = _quality_gate(workspace)
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
    artifacts = _validated_list(
        ArtifactDraftSpec,
        _canonical_json(workspace, "artifact-plan"),
        label="artifact plan",
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
    course = _canonical_json(workspace, "course")
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
        blueprint = CourseSkillBlueprint(
            name=course["name"],
            title=course["title"],
            description=course["description"],
            scope=course["scope"],
            artifact_language=course["artifact_language"],
            interaction=CourseInteraction.model_validate(
                _canonical_json(workspace, "interaction")
            ),
            capability_profile=CapabilityProfile.model_validate(
                _canonical_json(workspace, "capability-profile")
            ),
            curriculum=CurriculumDesign.model_validate(
                _canonical_json(workspace, "curriculum")
            ),
            prerequisites=course.get("prerequisites", []),
            core_principles=[
                CorePrinciple.model_validate(item)
                for item in course.get("core_principles", [])
            ],
            semantic_units=semantic_units,
            semantic_relations=semantic_relations,
            artifacts=rendered_artifacts,
            assets=assets,
            sources=[
                CourseSkillSource.model_validate(item)
                for item in raw_sources
            ],
            coverage_ledger=CourseCoverageLedger.model_validate(seed["coverage_ledger"]),
            claims=claims,
            limitations=list(dict.fromkeys([*seed_limitations, *course_limitations])),
        )
    except (KeyError, TypeError, PydanticValidationError, ValueError) as exc:
        raise ProcessingError(f"Canonical workspace state cannot compile a Skill: {exc}") from exc
    validate_blueprint_against_workspace(blueprint, workspace)
    build_id = course_skill_build_id(blueprint)
    semantic_record = workspace.canonical_record("semantic-map")
    curriculum_record = workspace.canonical_record("curriculum")
    affordance_record = workspace.canonical_record("instructional-affordances")
    assert semantic_record is not None
    assert curriculum_record is not None
    assert affordance_record is not None
    receipt = WorkspaceBuildReceipt(
        build_id=build_id,
        workspace_snapshot_digest=workspace.workspace_snapshot_digest(),
        name=blueprint.name,
        semantic_map_digest=semantic_record.digest,
        curriculum_digest=curriculum_record.digest,
        instructional_affordance_digest=affordance_record.digest,
        critic_report_digest=critic_digest,
        behavior_report_digest=behavior_digest,
        artifacts=artifact_references,
    )
    build_directory = workspace.root / "builds" / build_id
    atomic_write_json(build_directory / "blueprint.json", receipt)
    atomic_write_json(
        build_directory / "critic-report.json",
        _canonical_json(workspace, "critic-report"),
    )
    atomic_write_json(
        build_directory / "behavior-report.json",
        _canonical_json(workspace, "behavior-report"),
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
    generated = render_course_skill_package(
        blueprint,
        output,
        workspace_root=workspace.root,
    )
    report = validate_skill(
        generated,
        run_official=run_official_validation,
        check_code=True,
    )
    validation_path = build_directory / "validation-report.json"
    atomic_write_json(validation_path, report)
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
        generated_path=generated,
        installed_path=installed,
        installation_status=status,
        host=host,
        scope="project" if project else "user",
        course_coverage=(
            blueprint.coverage_ledger.state
            if blueprint.coverage_ledger is not None
            else "unproven"
        ),
        validation_report_path=validation_path,
    )
