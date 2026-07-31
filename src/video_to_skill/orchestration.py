"""Strict result contracts for workspace-centered agent reasoning."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from video_to_skill.generation import (
    CapabilityLevel,
    CapabilityProfile,
    CorePrinciple,
    CourseAsset,
    CourseInteraction,
    CourseSkillClaim,
    CurriculumDesign,
    SemanticRelation,
    SemanticUnit,
    SkillMode,
)
from video_to_skill.models import ObservationProducer


class OrchestrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapabilityEvidence(OrchestrationModel):
    mode: SkillMode
    ceiling: CapabilityLevel
    semantic_unit_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1, max_length=1200)

    @field_validator("semantic_unit_ids")
    @classmethod
    def unique_unit_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("capability evidence semantic unit ids must be unique")
        return value


class SemanticConflict(OrchestrationModel):
    id: str = Field(min_length=1, max_length=160)
    semantic_unit_ids: list[str] = Field(min_length=2)
    summary: str = Field(min_length=1, max_length=1200)
    material: bool = True
    resolved: bool = False
    resolution: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def coherent_resolution(self) -> SemanticConflict:
        if self.resolved != (self.resolution is not None):
            raise ValueError("resolved semantic conflicts require a resolution")
        return self


class SemanticCoverage(OrchestrationModel):
    source_ids: list[str] = Field(min_length=1)
    core_units: int = Field(ge=0)
    supporting_units: int = Field(ge=0)
    contextual_units: int = Field(ge=0)
    incidental_units: int = Field(ge=0)
    included_units: int = Field(ge=0)
    merged_units: int = Field(ge=0)
    context_only_units: int = Field(ge=0)
    omitted_units: int = Field(ge=0)
    material_units_accounted_for: bool


class AnalyzeResult(OrchestrationModel):
    schema_version: Literal[1] = 1
    task_id: str
    lease_token: str = Field(min_length=20)
    snapshot_digest: str
    producer: ObservationProducer
    integrated: bool
    semantic_units: list[SemanticUnit] = Field(min_length=1)
    semantic_relations: list[SemanticRelation] = Field(default_factory=list)
    capability_evidence: list[CapabilityEvidence] = Field(min_length=4, max_length=4)
    conflicts: list[SemanticConflict] = Field(default_factory=list)
    coverage: SemanticCoverage

    @model_validator(mode="after")
    def coherent_graph(self) -> AnalyzeResult:
        unit_ids = [unit.id for unit in self.semantic_units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("semantic unit ids must be unique")
        known = set(unit_ids)
        relation_keys: set[tuple[str, str, str]] = set()
        for relation in self.semantic_relations:
            if relation.from_unit_id not in known or relation.to_unit_id not in known:
                raise ValueError("semantic relations must reference submitted semantic units")
            key = (relation.from_unit_id, relation.to_unit_id, relation.kind)
            if key in relation_keys:
                raise ValueError("semantic relations cannot contain duplicates")
            relation_keys.add(key)
        modes = [item.mode for item in self.capability_evidence]
        if set(modes) != {"learn", "practice", "apply", "reference"} or len(set(modes)) != 4:
            raise ValueError("capability evidence must cover each behavior exactly once")
        for item in self.capability_evidence:
            if not set(item.semantic_unit_ids) <= known:
                raise ValueError("capability evidence references unknown semantic units")
        for conflict in self.conflicts:
            if not set(conflict.semantic_unit_ids) <= known:
                raise ValueError("semantic conflicts reference unknown semantic units")
        material = [
            unit
            for unit in self.semantic_units
            if unit.materiality in {"core", "supporting"}
        ]
        if self.coverage.material_units_accounted_for and any(
            unit.disposition not in {"included", "merged", "context-only", "omitted"}
            for unit in material
        ):
            raise ValueError("material semantic units are not fully accounted for")
        return self


AffordanceKind = Literal[
    "learning-objectives",
    "misconceptions",
    "retrieval-prompts",
    "transfer-prompts",
    "focused-exercises",
    "success-criteria",
    "scored-rubric",
    "progressive-hints",
    "retry-loop",
    "capstone",
    "operational-playbook",
    "expected-state",
    "validation",
    "recovery",
    "quick-reference",
    "decision-rules",
]
AffordanceStatus = Literal["provided", "unsupported", "not-applicable"]
ArtifactDisclosure = Literal["normal", "after-attempt"]
_ROOT_ARTIFACTS = {
    "learning-path.md",
    "glossary.md",
    "patterns.md",
    "cheatsheet.md",
}

AFFORDANCE_CATALOG: dict[SkillMode, tuple[AffordanceKind, ...]] = {
    "learn": (
        "learning-objectives",
        "misconceptions",
        "retrieval-prompts",
        "transfer-prompts",
    ),
    "practice": (
        "focused-exercises",
        "success-criteria",
        "scored-rubric",
        "progressive-hints",
        "retry-loop",
        "capstone",
    ),
    "apply": (
        "operational-playbook",
        "expected-state",
        "validation",
        "recovery",
    ),
    "reference": (
        "quick-reference",
        "decision-rules",
    ),
}

REQUIRED_AFFORDANCES: dict[
    SkillMode,
    dict[CapabilityLevel, frozenset[AffordanceKind]],
] = {
    "learn": {
        "strong": frozenset(AFFORDANCE_CATALOG["learn"]),
        "medium": frozenset(
            {"learning-objectives", "retrieval-prompts", "transfer-prompts"}
        ),
        "light": frozenset({"retrieval-prompts"}),
        "unsupported": frozenset(),
    },
    "practice": {
        "strong": frozenset(AFFORDANCE_CATALOG["practice"]),
        "medium": frozenset(
            {"focused-exercises", "success-criteria", "scored-rubric", "retry-loop"}
        ),
        "light": frozenset({"focused-exercises", "success-criteria"}),
        "unsupported": frozenset(),
    },
    "apply": {
        "strong": frozenset(AFFORDANCE_CATALOG["apply"]),
        "medium": frozenset(
            {"operational-playbook", "expected-state", "validation"}
        ),
        "light": frozenset({"operational-playbook"}),
        "unsupported": frozenset(),
    },
    "reference": {
        "strong": frozenset(AFFORDANCE_CATALOG["reference"]),
        "medium": frozenset({"quick-reference"}),
        "light": frozenset({"quick-reference"}),
        "unsupported": frozenset(),
    },
}


class InstructionalAffordance(OrchestrationModel):
    id: str = Field(min_length=1, max_length=160)
    mode: SkillMode
    kind: AffordanceKind
    status: AffordanceStatus
    artifact_ids: list[str] = Field(default_factory=list)
    semantic_unit_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1, max_length=1200)

    @model_validator(mode="after")
    def coherent_status(self) -> InstructionalAffordance:
        if self.kind not in AFFORDANCE_CATALOG[self.mode]:
            raise ValueError("instructional affordance kind does not belong to its mode")
        if self.status == "provided":
            if not self.artifact_ids or not self.semantic_unit_ids:
                raise ValueError(
                    "provided affordances require artifact and semantic-unit links"
                )
        elif self.artifact_ids:
            raise ValueError("unsupported affordances cannot reference artifacts")
        return self


class ArtifactDraftSpec(OrchestrationModel):
    id: str = Field(min_length=1, max_length=160)
    path: str
    title: str = Field(min_length=1, max_length=300)
    modes: list[SkillMode] = Field(min_length=1, max_length=4)
    disclosure: ArtifactDisclosure
    use_when: str = Field(min_length=1, max_length=500)
    independent_loading_reason: str = Field(min_length=1, max_length=500)
    semantic_unit_ids: list[str] = Field(min_length=1)
    affordance_ids: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list, max_length=30)
    draft_path: str
    draft_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def safe_artifact_path(cls, value: str) -> str:
        path = PurePosixPath(value.strip())
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or path.suffix.casefold() != ".md"
            or path.name in {"SKILL.md", "source-map.md", "sources.md"}
        ):
            raise ValueError("artifact path must be a safe relative Markdown path")
        if len(path.parts) == 1:
            if path.as_posix() not in _ROOT_ARTIFACTS:
                raise ValueError("root artifact path is not an allowed portable artifact")
        elif (
            len(path.parts) > 4
            or path.parts[0] in {"assets", "."}
            or any(
                not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", part)
                for part in path.parts
            )
        ):
            raise ValueError("artifact collection path is unsafe or too deeply nested")
        return path.as_posix()

    @field_validator("draft_path")
    @classmethod
    def safe_draft_path(cls, value: str) -> str:
        path = PurePosixPath(value.strip())
        if path.is_absolute() or ".." in path.parts or "\\" in value or path.suffix != ".md":
            raise ValueError("draft path must be a safe task-output-relative Markdown path")
        return path.as_posix()

    @field_validator("modes", "semantic_unit_ids", "affordance_ids", "topics")
    @classmethod
    def unique_lists(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("artifact list values must be unique")
        return value


class AuthorResult(OrchestrationModel):
    schema_version: Literal[1] = 1
    task_id: str
    lease_token: str = Field(min_length=20)
    snapshot_digest: str
    producer: ObservationProducer
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=1024)
    scope: str = Field(min_length=1, max_length=1000)
    artifact_language: str = Field(min_length=2, max_length=80)
    interaction: CourseInteraction
    capability_profile: CapabilityProfile
    curriculum: CurriculumDesign
    prerequisites: list[str] = Field(default_factory=list, max_length=30)
    core_principles: list[CorePrinciple] = Field(default_factory=list, max_length=24)
    artifacts: list[ArtifactDraftSpec] = Field(min_length=1)
    affordance_ledger: list[InstructionalAffordance]
    assets: list[CourseAsset] = Field(default_factory=list)
    claims: list[CourseSkillClaim] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    curriculum_decision_required: bool = False
    curriculum_decision_summary: str | None = Field(default=None, max_length=1200)

    @field_validator("title", "description", "scope", "artifact_language")
    @classmethod
    def compact_text(cls, value: str) -> str:
        return " ".join(value.split())

    @model_validator(mode="after")
    def coherent_authoring(self) -> AuthorResult:
        artifact_ids = [artifact.id for artifact in self.artifacts]
        artifact_paths = [artifact.path for artifact in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("artifact ids must be unique")
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("artifact paths must be unique")
        known_artifacts = set(artifact_ids)
        affordance_ids = [item.id for item in self.affordance_ledger]
        if len(affordance_ids) != len(set(affordance_ids)):
            raise ValueError("instructional affordance ids must be unique")
        known_affordances = set(affordance_ids)
        ledger_keys = [(item.mode, item.kind) for item in self.affordance_ledger]
        expected_keys = {
            (mode, kind)
            for mode, kinds in AFFORDANCE_CATALOG.items()
            for kind in kinds
        }
        if set(ledger_keys) != expected_keys or len(ledger_keys) != len(expected_keys):
            raise ValueError(
                "instructional affordance ledger must cover every catalog entry exactly once"
            )
        for item in self.affordance_ledger:
            if not set(item.artifact_ids) <= known_artifacts:
                raise ValueError("instructional affordance references unknown artifacts")
        for artifact in self.artifacts:
            if not set(artifact.affordance_ids) <= known_affordances:
                raise ValueError("artifact references unknown instructional affordances")
        for path in self.curriculum.paths:
            if not set(path.artifact_ids) <= known_artifacts:
                raise ValueError("curriculum path references unknown artifacts")
            withheld = {
                artifact.id
                for artifact in self.artifacts
                if artifact.disclosure == "after-attempt"
            }
            if set(path.artifact_ids) & withheld:
                raise ValueError("curriculum paths cannot index after-attempt artifacts")
        if any(artifact.disclosure == "after-attempt" for artifact in self.artifacts) and not any(
            artifact.disclosure == "normal" and "practice" in artifact.modes
            for artifact in self.artifacts
        ):
            raise ValueError(
                "after-attempt artifacts require a normally disclosed practice artifact"
            )
        profile = self.capability_profile
        levels: dict[SkillMode, CapabilityLevel] = {
            "learn": profile.learn,
            "practice": profile.practice,
            "apply": profile.apply,
            "reference": profile.reference,
        }
        for mode, level in levels.items():
            provided = {
                item.kind
                for item in self.affordance_ledger
                if item.mode == mode and item.status == "provided"
            }
            missing = REQUIRED_AFFORDANCES[mode][level] - provided
            if missing:
                raise ValueError(
                    f"{level} {mode} capability lacks required affordances: "
                    + ", ".join(sorted(missing))
                )
            if level == "unsupported" and provided:
                raise ValueError(f"unsupported {mode} capability cannot provide affordances")
        if self.curriculum_decision_required != (
            self.curriculum_decision_summary is not None
        ):
            raise ValueError(
                "material curriculum decisions require a concise decision summary"
            )
        claim_ids = [claim.id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim ids must be unique")
        core_claim_ids = {item.claim_id for item in self.core_principles}
        if not core_claim_ids <= set(claim_ids):
            raise ValueError("core principles must reference submitted claims")
        return self


ReviewCategory = Literal[
    "semantic-retention",
    "instructional-affordance",
    "grounding",
    "disclosure",
    "runtime-behavior",
    "safety",
    "scope",
]
ReviewSeverity = Literal["info", "warning", "error"]
ReviewVerdict = Literal["pass", "fail"]


class ReviewFinding(OrchestrationModel):
    id: str = Field(min_length=1, max_length=160)
    category: ReviewCategory
    severity: ReviewSeverity
    target_kind: str = Field(min_length=1, max_length=120)
    target_id: str = Field(min_length=1, max_length=200)
    semantic_unit_ids: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1, max_length=1200)
    required_change: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def error_requires_change(self) -> ReviewFinding:
        if self.severity == "error" and self.required_change is None:
            raise ValueError("blocking review findings require a concrete change")
        return self


class BehaviorCheck(OrchestrationModel):
    id: str = Field(min_length=1, max_length=160)
    scenario: str = Field(min_length=1, max_length=800)
    passed: bool
    summary: str = Field(min_length=1, max_length=1200)


class ReviewResult(OrchestrationModel):
    schema_version: Literal[1] = 1
    task_id: str
    lease_token: str = Field(min_length=20)
    snapshot_digest: str
    reviewed_snapshot_digest: str
    producer: ObservationProducer
    verdict: ReviewVerdict
    findings: list[ReviewFinding] = Field(default_factory=list)
    behavior_checks: list[BehaviorCheck] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_verdict(self) -> ReviewResult:
        has_errors = any(finding.severity == "error" for finding in self.findings)
        failed_behavior = any(not check.passed for check in self.behavior_checks)
        if self.verdict == "pass" and (has_errors or failed_behavior):
            raise ValueError("passing reviews cannot retain errors or failed behavior checks")
        if self.verdict == "fail" and not (has_errors or failed_behavior):
            raise ValueError("failing reviews require an error or failed behavior check")
        return self


ActionKind = Literal["dispatch-agent", "ask-user"]
RunStatus = Literal["actions-required", "complete"]


class RunAction(OrchestrationModel):
    kind: ActionKind
    task_id: str
    task_path: Path
    role: str
    persona_hint: str | None = None
    parallel_group: str | None = None
    already_leased: bool = False
    prompt: str | None = None
    options: list[dict[str, str]] = Field(default_factory=list)


class RunEnvelope(OrchestrationModel):
    status: RunStatus
    workspace: Path
    actions: list[RunAction] = Field(default_factory=list)
    completion: dict[str, object] | None = None

    @model_validator(mode="after")
    def coherent_status(self) -> RunEnvelope:
        if self.status == "actions-required":
            if not self.actions or self.completion is not None:
                raise ValueError("actions-required needs actions and no completion")
        elif self.actions or self.completion is None:
            raise ValueError("complete needs a completion and no actions")
        return self


class DecisionResult(OrchestrationModel):
    schema_version: Literal[1] = 1
    task_id: str
    lease_token: str = Field(min_length=20)
    snapshot_digest: str
    producer: ObservationProducer
    selected_path_id: str = Field(min_length=1, max_length=160)


class SubmissionReceipt(OrchestrationModel):
    task_id: str
    status: Literal["complete"] = "complete"
    result_digest: str
