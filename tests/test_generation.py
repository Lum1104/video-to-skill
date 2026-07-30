import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError as PydanticValidationError

from video_to_skill.errors import ProcessingError
from video_to_skill.generation import (
    COURSE_SKILL_MARKER,
    CapabilityProfile,
    CorePrinciple,
    CourseArtifact,
    CourseAsset,
    CourseInteraction,
    CourseSkillBlueprint,
    CourseSkillClaim,
    CourseSkillSource,
    CurriculumDesign,
    CurriculumPath,
    SemanticUnit,
    blueprint_from_json,
    render_course_skill_package,
)
from video_to_skill.validation import validate_skill


def test_generator_skill_owns_the_complete_user_workflow() -> None:
    generator = (Path(__file__).parents[1] / "SKILL.md").read_text(encoding="utf-8")
    assert "One invocation owns the complete workflow" in generator
    assert "process every accessible item by default" in generator
    assert "Do not create a second generic video tutor Skill" in generator
    assert "four behaviors into artifact quotas" in generator
    assert "Build the canonical semantic map" in generator
    assert "Use a thematic course as the default primary design" in generator
    assert "empty-invocation contract" in generator
    assert "blueprint-schema --workspace WORKSPACE --output AUTHORING_JSON" in generator
    assert "`blueprint_schema`" in generator
    assert "Preserve the seed's `sources` and `coverage_ledger` exactly" in generator
    assert "build-skill BLUEPRINT_JSON --host claude" in generator
    assert "build-skill BLUEPRINT_JSON --host codex" in generator


def _blueprint() -> CourseSkillBlueprint:
    evidence = {
        "source_id": "course-01",
        "start": 62,
        "end": 75,
        "modalities": ["speech", "visual", "temporal"],
        "evidence_ids": ["transcript-1", "frame-before", "frame-after"],
    }
    artifacts = [
        CourseArtifact(
            id="artifact-foundations",
            path="chapters/foundations.md",
            title="Foundations",
            modes=["learn", "reference"],
            disclosure="normal",
            use_when="learning the core model",
            independent_loading_reason="Load the foundational concept independently.",
            semantic_unit_ids=["unit-transition"],
            topics=["state transitions"],
            content=(
                "# Foundations\n\n## Core idea\n\nObserve both the action and its result.\n\n"
                "## Knowledge check\n\nWhat evidence establishes a transition?\n\n"
                "## Evidence\n\n`claim-foundation`\n"
            ),
        ),
        CourseArtifact(
            id="artifact-exercise",
            path="exercises/observe-transition.md",
            title="Observe a transition",
            modes=["practice"],
            disclosure="normal",
            use_when="checking whether the learner can distinguish intent from result",
            independent_loading_reason="Present practice without loading its solution.",
            semantic_unit_ids=["unit-transition"],
            topics=["evidence"],
            content=(
                "# Observe a transition\n\n## Task\n\nIdentify the before and after states.\n\n"
                "## Success criteria\n\nBoth states are grounded in frames.\n"
            ),
        ),
        CourseArtifact(
            id="artifact-solution",
            path="solutions/observe-transition.md",
            title="Observe a transition rubric",
            modes=["practice"],
            disclosure="after-attempt",
            use_when="grading an attempt",
            independent_loading_reason="Withhold the rubric until after an attempt.",
            semantic_unit_ids=["unit-transition"],
            content="# Rubric\n\nAward credit only when both states are cited.\n",
        ),
        CourseArtifact(
            id="artifact-application",
            path="playbooks/verify-transition.md",
            title="Verify a transition",
            modes=["apply"],
            disclosure="normal",
            use_when="applying the demonstrated verification method",
            independent_loading_reason="Load the procedure only for real application.",
            semantic_unit_ids=["unit-transition"],
            topics=["verification"],
            content=(
                "# Verify a transition\n\n## Procedure\n\n1. Capture the before and after "
                "states.\n\n## Verification\n\nConfirm both states are visible.\n"
            ),
        ),
        CourseArtifact(
            id="artifact-reference",
            path="reference/evidence-types.md",
            title="Evidence types",
            modes=["reference"],
            disclosure="normal",
            use_when="looking up which modality supports a claim",
            independent_loading_reason="Answer precise evidence questions without a chapter.",
            semantic_unit_ids=["unit-transition"],
            topics=["speech", "visual", "temporal"],
            content="# Evidence types\n\nUse temporal evidence for transitions.\n",
        ),
    ]
    claims = [
        CourseSkillClaim(
            id="claim-core",
            file="SKILL.md",
            kind="principle",
            summary="A transition requires before and after evidence.",
            inferred=False,
            confidence="high",
            semantic_unit_ids=["unit-transition"],
            evidence=[evidence],
        ),
        CourseSkillClaim(
            id="claim-foundation",
            file="chapters/foundations.md",
            kind="concept",
            summary="Actions and results need different evidence.",
            inferred=False,
            confidence="high",
            semantic_unit_ids=["unit-transition"],
            evidence=[evidence],
        ),
        CourseSkillClaim(
            id="claim-practice",
            file="exercises/observe-transition.md",
            kind="exercise-basis",
            summary="The demonstrated transition can be used for retrieval practice.",
            inferred=True,
            confidence="medium",
            semantic_unit_ids=["unit-transition"],
            evidence=[evidence],
        ),
        CourseSkillClaim(
            id="claim-solution",
            file="solutions/observe-transition.md",
            kind="rubric-basis",
            summary="A valid answer identifies both observable states.",
            inferred=True,
            confidence="medium",
            semantic_unit_ids=["unit-transition"],
            evidence=[evidence],
        ),
        CourseSkillClaim(
            id="claim-step",
            file="playbooks/verify-transition.md",
            kind="procedure-step",
            summary="Capture observable states before and after the action.",
            inferred=False,
            confidence="high",
            semantic_unit_ids=["unit-transition"],
            evidence=[evidence],
        ),
        CourseSkillClaim(
            id="claim-reference",
            file="reference/evidence-types.md",
            kind="reference",
            summary="Temporal evidence supports transition claims.",
            inferred=False,
            confidence="high",
            semantic_unit_ids=["unit-transition"],
            evidence=[evidence],
        ),
    ]
    return CourseSkillBlueprint(
        name="transition-course",
        title="Transition Course",
        description=(
            "Teach and apply the course's evidence-grounded transition method. "
            "Use for lessons, practice, project application, or precise reference."
        ),
        scope="Learn how to establish and verify observable state transitions.",
        artifact_language="English",
        interaction=CourseInteraction(
            welcome=(
                "Let's explore how observable transitions work. "
                'Tell me what you are examining—or say "start".'
            ),
            starter_questions=[
                "What state transition do you want to understand?",
                "What evidence can you currently observe?",
            ],
        ),
        capability_profile=CapabilityProfile(
            learn="strong",
            practice="strong",
            apply="strong",
            reference="strong",
            rationale="The source demonstrates and explains an observable transition.",
        ),
        curriculum=CurriculumDesign(
            selected_path_id="thematic",
            rationale="A thematic path connects the concept to practice and application.",
            paths=[
                CurriculumPath(
                    id="thematic",
                    title="Thematic transition course",
                    kind="thematic",
                    use_when="learning and applying the complete transition method",
                    artifact_ids=[
                        "artifact-foundations",
                        "artifact-exercise",
                        "artifact-application",
                        "artifact-reference",
                    ],
                )
            ],
        ),
        prerequisites=["Can inspect a before and after state."],
        core_principles=[
            CorePrinciple(
                text="Require before and after evidence for a transition.",
                claim_id="claim-core",
            )
        ],
        semantic_units=[
            SemanticUnit(
                id="unit-transition",
                source_id="course-01",
                start=62,
                end=75,
                speaker="Instructor",
                kind="claim",
                summary="A transition requires observable before and after states.",
                materiality="core",
                disposition="included",
                inferred=False,
                confidence="high",
                modalities=["speech", "visual", "temporal"],
                evidence_ids=["transcript-1", "frame-before", "frame-after"],
            )
        ],
        artifacts=artifacts,
        sources=[
            CourseSkillSource(
                id="course-01",
                title="Transition Demo",
                creator="Instructor",
                platform="youtube",
                url="https://youtu.be/example",
                coverage="complete",
            )
        ],
        claims=claims,
        limitations=["The course demonstrates one interface variant."],
    )


def _blueprint_with_asset(workspace: Path, source_path: Path) -> CourseSkillBlueprint:
    payload = _blueprint().model_dump(mode="json")
    for artifact in payload["artifacts"]:
        if artifact["path"] == "chapters/foundations.md":
            artifact["content"] += "\n![Observed state](../assets/observed-state.png)\n"
            break
    payload["assets"] = [
        CourseAsset(
            path="assets/observed-state.png",
            source_path=source_path,
            description="Observed before and after state",
            used_by=["chapters/foundations.md"],
            claim_ids=["claim-foundation"],
        ).model_dump(mode="json")
    ]
    return CourseSkillBlueprint.model_validate(payload)


def test_rendered_course_skill_is_teaching_first_and_valid(tmp_path: Path) -> None:
    target = render_course_skill_package(
        _blueprint(),
        tmp_path / "skills" / "transition-course",
        workspace_root=tmp_path / "workspace",
    )

    resident = (target / "SKILL.md").read_text(encoding="utf-8")
    assert COURSE_SKILL_MARKER in resident
    assert "## Empty invocation" in resident
    assert "at most three in one turn" in resident
    assert "**Learn:**" in resident
    assert "**Practice:**" in resident
    assert "**Apply:**" in resident
    assert "**Reference:**" in resident
    assert "without loading its solution" in resident
    assert "user's actual context" in resident
    assert "outside or current knowledge" in resident
    assert "solutions/observe-transition.md" not in resident
    assert (target / "source-map.md").is_file()
    assert (target / "build-manifest.json").is_file()

    sources = (target / "sources.md").read_text(encoding="utf-8")
    assert "https://youtu.be/example?t=62" in sources
    assert "speech+visual+temporal" in sources

    report = validate_skill(target, run_official=True)
    assert report.valid, report.model_dump()


def test_renderer_copies_only_sanitized_minimal_course_assets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "frames" / "state.jpg"
    source.parent.mkdir(parents=True)
    exif = Image.Exif()
    exif[0x010E] = "private workspace description"
    Image.new("RGB", (320, 180), "#2d5aa6").save(source, exif=exif)

    target = render_course_skill_package(
        _blueprint_with_asset(workspace, Path("frames/state.jpg")),
        tmp_path / "transition-course",
        workspace_root=workspace,
    )

    copied = target / "assets" / "observed-state.png"
    assert copied.is_file()
    with Image.open(copied) as image:
        assert image.format == "PNG"
        assert image.size == (320, 180)
        assert not image.getexif()
    assert source.is_file()
    assert "assets/observed-state.png" in (target / "sources.md").read_text(encoding="utf-8")
    assert validate_skill(target, run_official=False).valid


def test_blueprint_rejects_singular_modality(
    tmp_path: Path,
) -> None:
    payload = _blueprint().model_dump(mode="json")
    evidence = payload["claims"][0]["evidence"][0]
    evidence["modality"] = "speech+visual"
    del evidence["modalities"]
    with pytest.raises(PydanticValidationError):
        CourseSkillBlueprint.model_validate(payload)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.com/course",
        "https://example.com/course?access_token=secret",
        "https://example.com/course#access_token=secret",
        "https://example.com/course#AUTH_TOKEN=secret",
        "https://example.com/course#oauth_token=secret",
        "https://example.com/course#bearer_token=secret",
        "https://example.com/course#access%5Ftoken=secret",
        "https://example.com/course#oauth%255Ftoken=secret",
        "https://example.com/course#state=ok;access_token=secret",
        "https://example.com/course#state=ok%3Baccess_token=secret",
        "https://example.com/course#/callback?oauth_token=secret",
        "https://example.com/course?X-Amz-Signature=deadbeef",
        "https://example.com/course?expires=1999999999&sig=deadbeef",
        "https://example.com/course?api-key=secret",
    ],
)
def test_course_source_rejects_credentialed_or_temporary_urls(url: str) -> None:
    payload = _blueprint().model_dump(mode="json")
    payload["sources"][0]["url"] = url

    with pytest.raises(ValueError, match=r"userinfo|sensitive or temporary"):
        CourseSkillBlueprint.model_validate(payload)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123&list=course&index=2&t=15",
        "https://www.bilibili.com/video/BV123?spm_id_from=333.1007&vd_source=public",
        "https://example.com/course#lesson-2",
    ],
)
def test_course_source_preserves_normal_public_video_parameters(url: str) -> None:
    payload = _blueprint().model_dump(mode="json")
    payload["sources"][0]["url"] = url

    blueprint = CourseSkillBlueprint.model_validate(payload)

    assert blueprint.sources[0].url == url


@pytest.mark.parametrize("payload", ['{"name":', "{}"])
def test_blueprint_loader_wraps_invalid_json_as_processing_error(
    tmp_path: Path,
    payload: str,
) -> None:
    path = tmp_path / "blueprint.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ProcessingError, match="Invalid course skill blueprint"):
        blueprint_from_json(path)


def test_blueprint_validation_errors_never_echo_sensitive_url_input(
    tmp_path: Path,
) -> None:
    marker = "DO_NOT_ECHO_SECRET_7b4a"
    ordinary_fields = "&".join(f"k{index}=v" for index in range(257))
    sensitive_url = f"https://example.com/course?{ordinary_fields}&access_token={marker}"
    assert len(sensitive_url) < 4_000
    payload = _blueprint().model_dump(mode="json")
    payload["sources"][0]["url"] = sensitive_url

    with pytest.raises(PydanticValidationError) as direct_error:
        CourseSkillBlueprint.model_validate(payload)
    assert marker not in str(direct_error.value)
    assert "bounded query/fragment parameter limit" in str(direct_error.value)

    path = tmp_path / "sensitive-blueprint.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProcessingError) as wrapped_error:
        blueprint_from_json(path)
    assert marker not in str(wrapped_error.value)
    assert "Invalid course skill blueprint" in str(wrapped_error.value)


def test_renderer_keeps_workspace_and_shareable_skill_separate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    with pytest.raises(ProcessingError, match="must be separate"):
        render_course_skill_package(
            _blueprint(),
            workspace / "generated-skill",
            workspace_root=workspace,
        )
    assert not (workspace / "generated-skill").exists()


@pytest.mark.parametrize("extension", [".bmp", ".txt"])
def test_renderer_rejects_unsupported_asset_sources(
    tmp_path: Path,
    extension: str,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "frames" / f"state{extension}"
    source.parent.mkdir(parents=True)
    if extension == ".bmp":
        Image.new("RGB", (16, 16), "white").save(source)
    else:
        source.write_text("not an image", encoding="utf-8")

    with pytest.raises(ProcessingError, match="format is unsupported"):
        render_course_skill_package(
            _blueprint_with_asset(workspace, source),
            tmp_path / "transition-course",
            workspace_root=workspace,
        )


def test_renderer_rejects_asset_path_escape_and_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    Image.new("RGB", (16, 16), "white").save(outside)

    with pytest.raises(ProcessingError, match="escapes the evidence workspace"):
        render_course_skill_package(
            _blueprint_with_asset(workspace, outside),
            tmp_path / "escaped-course",
            workspace_root=workspace,
        )

    linked = workspace / "linked.png"
    linked.symlink_to(outside)
    with pytest.raises(ProcessingError, match="cannot use symlinks"):
        render_course_skill_package(
            _blueprint_with_asset(workspace, linked),
            tmp_path / "linked-course",
            workspace_root=workspace,
        )


def test_renderer_rejects_asset_dimension_over_bound(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "wide.png"
    workspace.mkdir()
    Image.new("RGB", (4097, 1), "white").save(source)

    with pytest.raises(ProcessingError, match="dimensions exceed"):
        render_course_skill_package(
            _blueprint_with_asset(workspace, source),
            tmp_path / "transition-course",
            workspace_root=workspace,
        )


def test_renderer_refuses_existing_destination(tmp_path: Path) -> None:
    target = tmp_path / "transition-course"
    target.mkdir()
    (target / "user-notes.md").write_text("preserve me", encoding="utf-8")

    with pytest.raises(ProcessingError, match="already exists"):
        render_course_skill_package(_blueprint(), target)
    assert (target / "user-notes.md").read_text(encoding="utf-8") == "preserve me"


def test_blueprint_does_not_require_artifacts_for_all_four_behaviors() -> None:
    payload = _blueprint().model_dump(mode="json")
    payload["artifacts"] = [
        item for item in payload["artifacts"] if item["id"] != "artifact-reference"
    ]
    payload["claims"] = [
        item for item in payload["claims"] if item["file"] != "reference/evidence-types.md"
    ]
    payload["curriculum"]["paths"][0]["artifact_ids"] = [
        artifact_id
        for artifact_id in payload["curriculum"]["paths"][0]["artifact_ids"]
        if artifact_id != "artifact-reference"
    ]

    blueprint = CourseSkillBlueprint.model_validate(payload)
    assert blueprint.capability_profile.reference == "strong"


def test_blueprint_requires_asset_link_and_visual_provenance(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "state.png"
    workspace.mkdir()
    Image.new("RGB", (16, 16), "white").save(source)
    linked = _blueprint_with_asset(workspace, source).model_dump(mode="json")

    for artifact in linked["artifacts"]:
        if artifact["path"] == "chapters/foundations.md":
            artifact["content"] = artifact["content"].replace(
                "\n![Observed state](../assets/observed-state.png)\n",
                "",
            )
    with pytest.raises(ValueError, match="does not link to asset"):
        CourseSkillBlueprint.model_validate(linked)

    ungrounded = _blueprint_with_asset(workspace, source).model_dump(mode="json")
    for claim in ungrounded["claims"]:
        if claim["id"] == "claim-foundation":
            claim["evidence"][0]["modalities"] = ["speech"]
    with pytest.raises(ValueError, match="needs a visual or temporal claim"):
        CourseSkillBlueprint.model_validate(ungrounded)


def test_course_contract_validator_rejects_missing_empty_invocation(
    tmp_path: Path,
) -> None:
    target = render_course_skill_package(_blueprint(), tmp_path / "transition-course")
    skill = target / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace(
            "## Empty invocation",
            "## Invocation",
        ),
        encoding="utf-8",
    )

    report = validate_skill(target, run_official=False)
    assert not report.valid
    assert "missing-empty-invocation" in {item.code for item in report.issues}


def test_v2_requires_material_semantic_coverage_and_disposition_reasons() -> None:
    uncovered = _blueprint().model_dump(mode="json")
    for artifact in uncovered["artifacts"]:
        artifact["semantic_unit_ids"] = ["unit-context"]
    uncovered["semantic_units"].append(
        {
            **uncovered["semantic_units"][0],
            "id": "unit-context",
            "materiality": "contextual",
            "disposition": "context-only",
            "disposition_reason": "Retained only in the source map.",
        }
    )
    with pytest.raises(ValueError, match="need a course artifact"):
        CourseSkillBlueprint.model_validate(uncovered)

    unexplained = _blueprint().model_dump(mode="json")
    unexplained["semantic_units"][0]["disposition"] = "omitted"
    with pytest.raises(ValueError, match="require a reason"):
        CourseSkillBlueprint.model_validate(unexplained)


def test_v2_requires_merge_chains_to_reach_artifact_linked_included_units() -> None:
    self_referential = _blueprint().model_dump(mode="json")
    self_referential["semantic_units"][0].update(
        {
            "disposition": "merged",
            "disposition_reason": "Duplicate wording.",
            "merged_into": "unit-transition",
        }
    )
    with pytest.raises(ValueError, match="merge chain contains a cycle"):
        CourseSkillBlueprint.model_validate(self_referential)

    cyclic = _blueprint().model_dump(mode="json")
    cyclic["semantic_units"][0].update(
        {
            "disposition": "merged",
            "disposition_reason": "Duplicate wording.",
            "merged_into": "unit-cycle",
        }
    )
    cyclic["semantic_units"].append(
        {
            **cyclic["semantic_units"][0],
            "id": "unit-cycle",
            "merged_into": "unit-transition",
        }
    )
    with pytest.raises(ValueError, match="merge chain contains a cycle"):
        CourseSkillBlueprint.model_validate(cyclic)

    omitted_terminal = _blueprint().model_dump(mode="json")
    omitted_terminal["semantic_units"][0].update(
        {
            "disposition": "merged",
            "disposition_reason": "Consolidated into the retained unit.",
            "merged_into": "unit-omitted",
        }
    )
    omitted_terminal["semantic_units"].append(
        {
            **omitted_terminal["semantic_units"][0],
            "id": "unit-omitted",
            "disposition": "omitted",
            "disposition_reason": "Not useful to the curriculum.",
            "merged_into": None,
        }
    )
    with pytest.raises(ValueError, match="must terminate at an included unit"):
        CourseSkillBlueprint.model_validate(omitted_terminal)

    unrepresented_terminal = _blueprint().model_dump(mode="json")
    unrepresented_terminal["semantic_units"][0].update(
        {
            "disposition": "merged",
            "disposition_reason": "Consolidated into the retained unit.",
            "merged_into": "unit-context",
        }
    )
    unrepresented_terminal["semantic_units"].append(
        {
            **unrepresented_terminal["semantic_units"][0],
            "id": "unit-context",
            "materiality": "contextual",
            "disposition": "included",
            "disposition_reason": None,
            "merged_into": None,
        }
    )
    with pytest.raises(ValueError, match="artifact-linked units"):
        CourseSkillBlueprint.model_validate(unrepresented_terminal)

    valid = _blueprint().model_dump(mode="json")
    valid["semantic_units"][0].update(
        {
            "disposition": "merged",
            "disposition_reason": "Consolidated into the retained unit.",
            "merged_into": "unit-retained",
        }
    )
    valid["semantic_units"].append(
        {
            **valid["semantic_units"][0],
            "id": "unit-retained",
            "disposition": "included",
            "disposition_reason": None,
            "merged_into": None,
        }
    )
    for artifact in valid["artifacts"]:
        artifact["semantic_unit_ids"] = ["unit-retained"]

    blueprint = CourseSkillBlueprint.model_validate(valid)
    assert blueprint.semantic_units[0].merged_into == "unit-retained"


def test_v2_allows_evidence_driven_artifact_collections() -> None:
    payload = _blueprint().model_dump(mode="json")
    for artifact in payload["artifacts"]:
        if artifact["id"] == "artifact-foundations":
            artifact["path"] = "founder-decisions/foundations.md"
    for claim in payload["claims"]:
        if claim["file"] == "chapters/foundations.md":
            claim["file"] = "founder-decisions/foundations.md"

    blueprint = CourseSkillBlueprint.model_validate(payload)

    assert blueprint.artifacts[0].path == "founder-decisions/foundations.md"


def test_v2_withholds_after_attempt_artifacts_independently_of_path(
    tmp_path: Path,
) -> None:
    payload = _blueprint().model_dump(mode="json")
    for artifact in payload["artifacts"]:
        if artifact["id"] == "artifact-solution":
            artifact["path"] = "answer-keys/observe-transition.md"
    for claim in payload["claims"]:
        if claim["file"] == "solutions/observe-transition.md":
            claim["file"] = "answer-keys/observe-transition.md"

    blueprint = CourseSkillBlueprint.model_validate(payload)
    target = render_course_skill_package(blueprint, tmp_path / "transition-course")
    resident = (target / "SKILL.md").read_text(encoding="utf-8")

    assert "answer-keys/observe-transition.md" not in resident
    assert "intentionally unindexed" in resident
    assert (target / "answer-keys" / "observe-transition.md").is_file()

    payload["curriculum"]["paths"][0]["artifact_ids"].append("artifact-solution")
    with pytest.raises(ValueError, match="cannot index after-attempt artifacts"):
        CourseSkillBlueprint.model_validate(payload)


def test_build_manifest_detects_human_modified_generated_file(tmp_path: Path) -> None:
    target = render_course_skill_package(_blueprint(), tmp_path / "transition-course")
    chapter = target / "chapters" / "foundations.md"
    chapter.write_text(
        chapter.read_text(encoding="utf-8") + "\nHuman edit.\n",
        encoding="utf-8",
    )

    report = validate_skill(target, run_official=False)

    assert not report.valid
    assert "managed-file-modified" in {item.code for item in report.issues}
