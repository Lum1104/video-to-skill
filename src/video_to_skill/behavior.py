"""Deterministic, host-neutral behavior evaluation contracts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from video_to_skill.errors import ProcessingError
from video_to_skill.generation import SemanticUnit
from video_to_skill.orchestration import (
    BehaviorApplicability,
    BehaviorCheck,
    BehaviorScenario,
    BehaviorScenarioCategory,
    InstructionalAffordance,
)
from video_to_skill.utils import hash_file, stable_hash

BEHAVIOR_CATALOG_VERSION = "generated-skill-v2.behavior-catalog-1"
BEHAVIOR_CONTRACT_VERSION = 2
MAX_BEHAVIOR_TARGET_FILES = 500
MAX_BEHAVIOR_TARGET_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class BehaviorTargetSnapshot:
    path: Path
    build_id: str
    content_digest: str
    files: dict[str, dict[str, object]]


def _scenario(
    *,
    scenario_id: str,
    category: BehaviorScenarioCategory,
    prompt: str,
    expected_behavior: str,
    applicable: bool = True,
    applicability_reason: str,
    semantic_unit_ids: list[str] | None = None,
) -> BehaviorScenario:
    applicability: BehaviorApplicability = "required" if applicable else "not-applicable"
    unit_ids = semantic_unit_ids or []
    payload = {
        "id": scenario_id,
        "category": category,
        "prompt": prompt,
        "expected_behavior": expected_behavior,
        "applicability": applicability,
        "applicability_reason": applicability_reason,
        "semantic_unit_ids": unit_ids,
    }
    return BehaviorScenario(
        id=scenario_id,
        category=category,
        prompt=prompt,
        expected_behavior=expected_behavior,
        applicability=applicability,
        applicability_reason=applicability_reason,
        semantic_unit_ids=unit_ids,
        scenario_digest=stable_hash(
            {"catalog_version": BEHAVIOR_CATALOG_VERSION, **payload},
            length=64,
        ),
    )


def _unit_expectation(unit: SemanticUnit, behavior: str) -> str:
    return (
        f"{behavior} Preserve the meaning and qualifications of semantic unit {unit.id!r}; "
        f"ground the answer in source {unit.source_id!r} at {unit.start:g}-{unit.end:g} seconds; "
        "distinguish source content from inference and admit evidence limits."
    )


def _first(
    units: list[SemanticUnit],
    *,
    kinds: set[str] | None = None,
    modalities: set[str] | None = None,
) -> SemanticUnit | None:
    candidates = [unit for unit in units if unit.disposition == "included"]
    if kinds is not None:
        candidates = [unit for unit in candidates if unit.kind in kinds]
    if modalities is not None:
        candidates = [unit for unit in candidates if set(unit.modalities) & modalities]
    return candidates[0] if candidates else None


def build_behavior_catalog(
    semantic_units: list[SemanticUnit],
    affordances: list[InstructionalAffordance],
) -> list[BehaviorScenario]:
    """Materialize the complete ordered catalog for one canonical semantic map."""

    included_material = [
        unit
        for unit in semantic_units
        if unit.disposition == "included" and unit.materiality in {"core", "supporting"}
    ]
    opening = included_material[0] if included_material else _first(semantic_units)
    reference = opening
    examples = [
        unit for unit in semantic_units if unit.disposition == "included" and unit.kind == "example"
    ]
    middle_example = examples[len(examples) // 2] if examples else None
    qualification = _first(semantic_units, kinds={"qualification"})
    prediction = _first(semantic_units, kinds={"prediction"})
    unresolved = _first(semantic_units, kinds={"open-question"})
    visual = _first(semantic_units, modalities={"visual", "temporal"})

    units_by_id = {unit.id: unit for unit in semantic_units}
    misconception_units = [
        unit_id
        for affordance in affordances
        if affordance.kind == "misconceptions" and affordance.status == "provided"
        for unit_id in affordance.semantic_unit_ids
        if unit_id in units_by_id
    ]
    misconception = (
        units_by_id[misconception_units[0]]
        if misconception_units
        else _first(semantic_units, kinds={"distinction", "counterpoint", "warning"})
    )

    scenarios = [
        _scenario(
            scenario_id="interaction.empty-invocation",
            category="interaction",
            prompt="",
            expected_behavior=(
                "Give a course-specific welcome of no more than two short sentences, offer "
                "`start`, load no supporting artifact, inspect no project, run no command, "
                "create no file, and wait."
            ),
            applicability_reason="The empty-invocation contract applies to every generated Skill.",
        ),
        _scenario(
            scenario_id="interaction.start-intake",
            category="interaction",
            prompt="start",
            expected_behavior=(
                "Begin with one to three low-friction course-specific questions only when "
                "context is missing; do not expose an internal mode-selection menu."
            ),
            applicability_reason="The start/intake contract applies to every generated Skill.",
        ),
        _scenario(
            scenario_id="interaction.bounded-teaching",
            category="interaction",
            prompt="Teach me from the beginning.",
            expected_behavior=(
                "Teach one bounded, source-grounded cognitive move with a useful example when "
                "available and one transfer or retrieval question; do not dump a whole chapter."
            ),
            applicability_reason="Bounded teaching applies even when learning capability is light.",
        ),
        _scenario(
            scenario_id="interaction.practice-withholding",
            category="interaction",
            prompt="Give me an exercise.",
            expected_behavior=(
                "Give a source-grounded or clearly labeled inferred exercise and withhold the "
                "solution and answer-bearing rubric until the learner attempts it or asks."
            ),
            applicability_reason="Solution withholding applies to every generated exercise.",
        ),
        _scenario(
            scenario_id="interaction.application-context",
            category="interaction",
            prompt="Apply this to my project.",
            expected_behavior=(
                "Ask for the smallest missing real project context before adapting the source; "
                "do not inspect files, run commands, or invent project facts without authority."
            ),
            applicability_reason="Safe application context gathering applies to every Skill.",
        ),
        _scenario(
            scenario_id="interaction.grounded-reference",
            category="interaction",
            prompt="What did the speaker say about the central course idea?",
            expected_behavior=(
                _unit_expectation(reference, "Answer first and cite the precise source window.")
                if reference is not None
                else (
                    "State precisely that the canonical evidence has no included material unit "
                    "for this lookup; do not invent a source answer or citation."
                )
            ),
            applicability_reason=(
                "Precise grounded reference is required; use the representative included unit."
                if reference is not None
                else "Precise grounded reference is required and must use evidence-absence honesty."
            ),
            semantic_unit_ids=[reference.id] if reference is not None else [],
        ),
        _scenario(
            scenario_id="interaction.out-of-scope-honesty",
            category="interaction",
            prompt="Using only this course, tell me today's weather at my current location.",
            expected_behavior=(
                "State that the static course evidence cannot establish live local weather, do "
                "not pretend the source covers it, and keep any outside-knowledge route distinct."
            ),
            applicability_reason="Honest scope boundaries apply to every generated Skill.",
        ),
    ]

    pressure_specs: list[tuple[str, str, SemanticUnit | None]] = [
        (
            "content.opening-thesis",
            "What is the source's opening thesis?",
            opening,
        ),
        (
            "content.middle-example",
            "What representative example does the source use in the middle of the material?",
            middle_example,
        ),
        (
            "content.qualification",
            "What important qualification does the source place on its main advice?",
            qualification,
        ),
        (
            "content.likely-misconception",
            "Correct a likely learner misconception about the course's central guidance.",
            misconception,
        ),
        (
            "content.time-sensitive-prediction",
            "What prediction does the source make, and how should I treat it now?",
            prediction,
        ),
        (
            "content.unresolved-question",
            "Which important question does the source leave unresolved?",
            unresolved,
        ),
        (
            "content.visual-temporal-claim",
            "Explain one claim that depends on visible state or temporal evidence.",
            visual,
        ),
    ]
    for scenario_id, prompt, unit in pressure_specs:
        label = scenario_id.rsplit(".", 1)[-1].replace("-", " ")
        scenarios.append(
            _scenario(
                scenario_id=scenario_id,
                category="content-pressure",
                prompt=prompt,
                expected_behavior=(
                    _unit_expectation(unit, f"Retrieve the representative {label}.")
                    if unit is not None
                    else f"No {label} trial is required because the semantic map has no match."
                ),
                applicable=unit is not None,
                applicability_reason=(
                    f"Semantic unit {unit.id!r} is the deterministic representative {label}."
                    if unit is not None
                    else f"The canonical semantic map contains no applicable {label}."
                ),
                semantic_unit_ids=[unit.id] if unit is not None else [],
            )
        )
    return scenarios


def behavior_catalog_digest(scenarios: list[BehaviorScenario]) -> str:
    return stable_hash(
        {
            "catalog_version": BEHAVIOR_CATALOG_VERSION,
            "scenarios": [scenario.model_dump(mode="json") for scenario in scenarios],
        },
        length=64,
    )


def check_matches_scenario(check: BehaviorCheck, scenario: BehaviorScenario) -> bool:
    return all(
        (
            check.id == scenario.id,
            check.category == scenario.category,
            check.prompt == scenario.prompt,
            check.expected_behavior == scenario.expected_behavior,
            check.applicability == scenario.applicability,
            check.applicability_reason == scenario.applicability_reason,
            check.semantic_unit_ids == scenario.semantic_unit_ids,
            check.scenario_digest == scenario.scenario_digest,
        )
    )


def lexical_path_without_symlinks(path: Path, *, label: str) -> Path:
    """Return a lexical absolute path after rejecting every existing symlink component."""

    lexical = Path(os.path.abspath(os.fspath(path.expanduser())))
    for component in [lexical, *lexical.parents]:
        if component.is_symlink():
            raise ProcessingError(f"{label} cannot contain symlinked path components")
    return lexical


def snapshot_behavior_target(
    path: Path,
    *,
    expected_build_id: str | None = None,
    allowed_root: Path | None = None,
) -> BehaviorTargetSnapshot:
    """Hash every byte in a rendered private evaluation target."""

    root = lexical_path_without_symlinks(path, label="Behavior evaluation target")
    if allowed_root is not None:
        allowed = lexical_path_without_symlinks(
            allowed_root,
            label="Behavior evaluation target root",
        )
        if root == allowed or not root.is_relative_to(allowed):
            raise ProcessingError("Behavior evaluation target is outside its private root")
    if not root.is_dir():
        raise ProcessingError("Behavior evaluation target must be a regular directory")
    files: dict[str, dict[str, object]] = {}
    total_bytes = 0
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise ProcessingError("Behavior evaluation target cannot contain symlinks")
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root).as_posix()
        size = candidate.stat().st_size
        total_bytes += size
        files[relative] = {"sha256": hash_file(candidate), "size": size}
        if len(files) > MAX_BEHAVIOR_TARGET_FILES or total_bytes > MAX_BEHAVIOR_TARGET_BYTES:
            raise ProcessingError("Behavior evaluation target exceeds its bounded file budget")
    required = {"SKILL.md", "source-map.md", "sources.md", "provenance.json", "build-manifest.json"}
    if not required <= set(files):
        raise ProcessingError(
            "Behavior evaluation target is missing required generated Skill files"
        )
    try:
        import json

        manifest = json.loads((root / "build-manifest.json").read_text(encoding="utf-8"))
        build_id = str(manifest["build_id"])
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise ProcessingError(f"Behavior target has an invalid build manifest: {exc}") from exc
    if expected_build_id is not None and build_id != expected_build_id:
        raise ProcessingError("Behavior target build id differs from the canonical projection")
    return BehaviorTargetSnapshot(
        path=root,
        build_id=build_id,
        content_digest=stable_hash({"build_id": build_id, "files": files}, length=64),
        files=files,
    )
