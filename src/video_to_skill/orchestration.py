"""Strict result contracts for workspace-centered agent reasoning."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from video_to_skill.generation import (
    CapabilityLevel,
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
