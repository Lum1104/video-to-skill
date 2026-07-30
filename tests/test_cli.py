import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from video_to_skill.cli import app
from video_to_skill.config import Settings
from video_to_skill.generation import (
    MAX_BLUEPRINT_AUTHORING_JSON_BYTES,
    CapabilityProfile,
    CorePrinciple,
    CourseArtifact,
    CourseInteraction,
    CourseSkillBlueprint,
    CourseSkillClaim,
    CourseSkillSource,
    CurriculumDesign,
    CurriculumPath,
    SemanticUnit,
    coverage_ledger_from_workspace,
)
from video_to_skill.models import (
    CaptionTrack,
    InspectionBatch,
    InspectionCompleteness,
    InspectionEntry,
    InspectionEntryStatus,
    SourceDescriptor,
    SourcePlatform,
)
from video_to_skill.workspace import Workspace

runner = CliRunner()


def _contract_workspace(tmp_path: Path, *, local: bool = False) -> Workspace:
    locator = str(tmp_path / "private" / "course.mp4") if local else "https://course.test/all"
    platform = SourcePlatform.LOCAL if local else SourcePlatform.YOUTUBE
    workspace = Workspace.create(
        root=tmp_path / "contract-workspace",
        inputs=[locator],
        settings=Settings(cache_root=tmp_path / "cache"),
    )
    sources = [
        SourceDescriptor(
            id=f"source-{index}",
            platform=platform,
            locator=(locator if local else f"https://youtu.be/lesson-{index}"),
            canonical_url=(None if local else f"https://youtu.be/lesson-{index}"),
            title=f"Lesson {index}",
        )
        for index in (1, 2)
    ]
    retired = SourceDescriptor(
        id="source-retired",
        platform=platform,
        locator=(locator if local else "https://youtu.be/retired"),
        canonical_url=(None if local else "https://youtu.be/retired"),
        title="Retired lesson",
    )
    workspace.upsert_sources([*sources, retired])
    workspace.upsert_sources(sources, prune=True, retirement_reason="removed from current course")
    workspace.replace_inspection_reports(
        [
            InspectionCompleteness(
                locator=locator,
                platform=platform,
                expected_entries=4,
                accessible_entries=2,
                inaccessible_entries=1,
                failed_entries=1,
                completeness_proven=False,
                disclaimer="One lesson is private and one metadata lookup failed.",
                entries=[
                    *[
                        InspectionEntry(
                            ordinal=index,
                            status=InspectionEntryStatus.ACCESSIBLE,
                            source_id=source.id,
                            title=source.title,
                            locator=source.canonical_url or source.locator,
                        )
                        for index, source in enumerate(sources, start=1)
                    ],
                    InspectionEntry(
                        ordinal=3,
                        status=InspectionEntryStatus.INACCESSIBLE,
                        title="Private lesson",
                        reason="private video",
                    ),
                    InspectionEntry(
                        ordinal=4,
                        status=InspectionEntryStatus.FAILED,
                        reason="metadata lookup failed",
                    ),
                ],
            )
        ]
    )
    return workspace


def _contract_blueprint(
    workspace: Workspace,
    *,
    omit_source_ids: set[str] | None = None,
) -> CourseSkillBlueprint:
    omit_source_ids = omit_source_ids or set()
    active = [source for source in workspace.list_sources() if source.id not in omit_source_ids]
    evidence = {
        "source_id": active[0].id,
        "start": 1,
        "end": 2,
        "modalities": ["speech"],
        "evidence_ids": ["transcript-1"],
    }
    artifact_specs = [
        ("foundations", "chapters/foundations.md", "Foundations", "learn"),
        ("check", "exercises/check.md", "Check", "practice"),
        ("check-solution", "solutions/check.md", "Check rubric", "practice"),
        ("apply", "playbooks/apply.md", "Apply", "apply"),
        ("terms", "reference/terms.md", "Terms", "reference"),
    ]
    artifacts = [
        CourseArtifact(
            id=artifact_id,
            path=path,
            title=title,
            modes=[mode],
            disclosure=("after-attempt" if artifact_id == "check-solution" else "normal"),
            use_when=f"using {title.lower()}",
            independent_loading_reason=f"Load {title.lower()} only when needed.",
            semantic_unit_ids=["unit-core"],
            content=(
                "# Material\n\n## Procedure\n\n1. Verify the result.\n"
                if mode == "apply"
                else "# Material\n\nGrounded course material.\n"
            ),
        )
        for artifact_id, path, title, mode in artifact_specs
    ]
    claims = [
        CourseSkillClaim(
            id="claim-core",
            file="SKILL.md",
            kind="principle",
            summary="Verify observable results.",
            inferred=False,
            confidence="high",
            semantic_unit_ids=["unit-core"],
            evidence=[evidence],
        ),
        *[
            CourseSkillClaim(
                id=f"claim-{index}",
                file=artifact.path,
                kind=("procedure-step" if "apply" in artifact.modes else "teaching-material"),
                summary=f"Ground {artifact.title}.",
                inferred="practice" in artifact.modes,
                confidence="medium" if "practice" in artifact.modes else "high",
                semantic_unit_ids=["unit-core"],
                evidence=[evidence],
            )
            for index, artifact in enumerate(artifacts, start=1)
        ],
    ]
    return CourseSkillBlueprint(
        name="contract-course",
        title="Contract Course",
        description="Teach, practice, apply, and reference the fully accounted course.",
        scope="Use the demonstrated method with explicit evidence.",
        artifact_language="English",
        interaction=CourseInteraction(
            welcome='Let us explore grounded verification. Say "start" and I will guide you.',
            starter_questions=["What result do you need to verify?"],
        ),
        capability_profile=CapabilityProfile(
            learn="strong",
            practice="strong",
            apply="strong",
            reference="strong",
            rationale="The course explains and demonstrates the complete method.",
        ),
        curriculum=CurriculumDesign(
            selected_path_id="thematic",
            rationale="The thematic path connects the complete method.",
            paths=[
                CurriculumPath(
                    id="thematic",
                    title="Thematic course",
                    kind="thematic",
                    use_when="learning or applying the method",
                    artifact_ids=["foundations", "check", "apply", "terms"],
                )
            ],
        ),
        core_principles=[CorePrinciple(text="Verify observable results.", claim_id="claim-core")],
        semantic_units=[
            SemanticUnit(
                id="unit-core",
                source_id=active[0].id,
                start=1,
                end=2,
                kind="claim",
                summary="Verify observable results.",
                materiality="core",
                disposition="included",
                inferred=False,
                confidence="high",
                modalities=["speech"],
                evidence_ids=["transcript-1"],
            )
        ],
        artifacts=artifacts,
        sources=[
            CourseSkillSource(
                id=source.id,
                title=source.title,
                creator=source.creator,
                platform=source.platform.value,
                url=source.canonical_url,
                coverage="partial",
            )
            for source in active
        ],
        coverage_ledger=coverage_ledger_from_workspace(workspace),
        claims=claims,
    )


def test_version_command() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_query_inventory_command(tmp_path: Path) -> None:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    workspace.upsert_sources(
        [
            SourceDescriptor(
                id="source",
                platform=SourcePlatform.LOCAL,
                locator="/tmp/demo.mp4",
                title="Demo",
            )
        ]
    )
    result = runner.invoke(app, ["query", str(workspace.root), "--inventory"])
    assert result.exit_code == 0, result.output
    assert "Evidence Inventory" in result.stdout
    assert "Demo" in result.stdout


def test_inspect_json_includes_course_completeness(
    monkeypatch,
) -> None:
    source = SourceDescriptor(
        id="youtube-one",
        platform=SourcePlatform.YOUTUBE,
        locator="https://youtu.be/one",
        canonical_url="https://youtu.be/one",
        title="Lesson One",
        duration=60,
        captions=[CaptionTrack(language="en", extension="vtt")],
    )
    report = InspectionCompleteness(
        locator="https://youtube.com/playlist?list=course",
        platform=SourcePlatform.YOUTUBE,
        expected_entries=2,
        accessible_entries=1,
        inaccessible_entries=1,
        failed_entries=0,
        completeness_proven=False,
        disclaimer="One expected lesson is private.",
        entries=[
            InspectionEntry(
                ordinal=1,
                status=InspectionEntryStatus.ACCESSIBLE,
                source_id=source.id,
                title=source.title,
                locator=source.canonical_url,
            ),
            InspectionEntry(
                ordinal=2,
                status=InspectionEntryStatus.INACCESSIBLE,
                reason="private video",
            ),
        ],
    )
    monkeypatch.setattr(
        "video_to_skill.cli.inspect_inputs_with_completeness",
        lambda sources, settings: InspectionBatch(sources=[source], reports=[report]),
    )

    result = runner.invoke(
        app,
        ["inspect", "https://youtube.com/playlist?list=course", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["sources"][0]["id"] == source.id
    assert payload["completeness"][0]["expected_entries"] == 2
    assert payload["completeness"][0]["completeness_proven"] is False


def test_blueprint_schema_preseeds_bounded_private_safe_full_ledger(
    tmp_path: Path,
) -> None:
    workspace = _contract_workspace(tmp_path, local=True)

    result = runner.invoke(
        app,
        ["blueprint-schema", "--workspace", str(workspace.root)],
    )

    assert result.exit_code == 0, result.output
    assert len(result.stdout.encode("utf-8")) <= MAX_BLUEPRINT_AUTHORING_JSON_BYTES
    payload = json.loads(result.stdout)
    seed = payload["blueprint_seed"]
    assert payload["blueprint_schema"]["title"] == "CourseSkillBlueprint"
    assert {source["id"] for source in seed["sources"]} == {"source-1", "source-2"}
    statuses = {entry["status"] for entry in seed["coverage_ledger"]["entries"]}
    assert {"accessible", "retired", "inaccessible", "failed"} <= statuses
    assert str(tmp_path / "private" / "course.mp4") not in result.stdout
    assert "transcript-1" not in result.stdout


def test_clean_command_removes_only_cache_artifacts(tmp_path: Path) -> None:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    source_dir = workspace.source_directory("source")
    media = source_dir / "media.mp4"
    caption = source_dir / "media.vtt"
    media.write_bytes(b"media")
    caption.write_text("WEBVTT", encoding="utf-8")
    result = runner.invoke(app, ["clean", str(workspace.root), "--yes"])
    assert result.exit_code == 0, result.output
    assert not media.exists()
    assert caption.exists()
    assert workspace.database_path.exists()


def test_install_generated_command_registers_project_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "SKILL.md").write_text(
        """---
name: course-skill
description: Teach and apply the grounded course.
---

# Course
""",
        encoding="utf-8",
    )
    (generated / "sources.md").write_text("# Sources\n", encoding="utf-8")
    (generated / "provenance.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sources": [
                    {
                        "id": "youtube-course",
                        "title": "Course",
                        "url": "https://youtu.be/course",
                    }
                ],
                "semantic_units": [
                    {
                        "id": "unit-1",
                        "source_id": "youtube-course",
                        "start": 1,
                        "end": 2,
                        "kind": "claim",
                        "summary": "Teach the grounded course.",
                        "materiality": "core",
                        "disposition": "included",
                        "inferred": False,
                        "confidence": "high",
                        "modalities": ["speech"],
                        "evidence_ids": ["transcript-1"],
                    }
                ],
                "semantic_relations": [],
                "claims": [
                    {
                        "id": "claim-1",
                        "file": "SKILL.md",
                        "kind": "concept",
                        "summary": "Teach the grounded course.",
                        "inferred": False,
                        "confidence": "high",
                        "semantic_unit_ids": ["unit-1"],
                        "evidence": [
                            {
                                "source_id": "youtube-course",
                                "start": 1,
                                "end": 2,
                                "modality": "speech",
                                "evidence_ids": ["transcript-1"],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "install-generated",
            str(generated),
            "--host",
            "codex",
            "--project",
            "--skip-official",
        ],
    )

    assert result.exit_code == 0, result.output
    target = tmp_path / ".agents/skills/course-skill"
    assert target.is_dir()
    assert "Codex" not in result.output
    assert "(codex, project)" in result.output


def test_build_skill_rejects_omitted_active_source_before_output_or_install(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _contract_workspace(tmp_path)
    blueprint = _contract_blueprint(workspace, omit_source_ids={"source-2"})
    blueprint_path = tmp_path / "omitted-source.json"
    blueprint_path.write_text(blueprint.model_dump_json(indent=2), encoding="utf-8")
    output = tmp_path / "must-not-render"
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "build-skill",
            str(blueprint_path),
            "--host",
            "codex",
            "--project",
            "--workspace",
            str(workspace.root),
            "--output",
            str(output),
            "--skip-official",
        ],
    )

    assert result.exit_code == 2
    assert "omits active workspace sources: source-2" in result.output
    assert "blueprint-schema --workspace" in result.output
    assert not output.exists()
    assert not (tmp_path / ".agents/skills/contract-course").exists()


@pytest.mark.parametrize("omitted_status", ["inaccessible", "failed"])
def test_build_skill_rejects_omitted_expected_inspection_entry(
    tmp_path: Path,
    monkeypatch,
    omitted_status: str,
) -> None:
    workspace = _contract_workspace(tmp_path)
    blueprint = _contract_blueprint(workspace)
    payload = blueprint.model_dump(mode="json")
    payload["coverage_ledger"]["entries"] = [
        entry
        for entry in payload["coverage_ledger"]["entries"]
        if not (entry["kind"] == "inspection-entry" and entry["status"] == omitted_status)
    ]
    tampered = CourseSkillBlueprint.model_validate(payload)
    blueprint_path = tmp_path / f"omitted-{omitted_status}.json"
    blueprint_path.write_text(tampered.model_dump_json(indent=2), encoding="utf-8")
    output = tmp_path / f"must-not-render-{omitted_status}"
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "build-skill",
            str(blueprint_path),
            "--host",
            "codex",
            "--project",
            "--workspace",
            str(workspace.root),
            "--output",
            str(output),
            "--skip-official",
        ],
    )

    assert result.exit_code == 2
    assert "coverage ledger omits entries" in result.output
    assert not output.exists()
    assert not (tmp_path / ".agents/skills/contract-course").exists()


def test_build_skill_rejects_false_complete_course_coverage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _contract_workspace(tmp_path)
    blueprint = _contract_blueprint(workspace)
    payload = blueprint.model_dump(mode="json")
    payload["coverage_ledger"]["state"] = "complete"
    payload["sources"][0]["coverage"] = "complete"
    tampered = CourseSkillBlueprint.model_validate(payload)
    blueprint_path = tmp_path / "false-complete.json"
    blueprint_path.write_text(tampered.model_dump_json(indent=2), encoding="utf-8")
    output = tmp_path / "must-not-render-complete"
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "build-skill",
            str(blueprint_path),
            "--host",
            "codex",
            "--project",
            "--workspace",
            str(workspace.root),
            "--output",
            str(output),
            "--skip-official",
        ],
    )

    assert result.exit_code == 2
    assert "course coverage state is 'complete'" in result.output
    assert "workspace proves 'partial'" in result.output
    assert "upgrades workspace coverage from 'partial' to 'complete'" in result.output
    assert not output.exists()
    assert not (tmp_path / ".agents/skills/contract-course").exists()


def test_build_skill_accepts_matching_full_workspace_inventory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _contract_workspace(tmp_path)
    blueprint = _contract_blueprint(workspace)
    blueprint_path = tmp_path / "matching.json"
    blueprint_path.write_text(blueprint.model_dump_json(indent=2), encoding="utf-8")
    output = tmp_path / "matched-course"
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "build-skill",
            str(blueprint_path),
            "--host",
            "codex",
            "--project",
            "--workspace",
            str(workspace.root),
            "--output",
            str(output),
            "--skip-official",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    completion = json.loads(result.stdout)
    assert completion["workspace_verified"] is True
    assert completion["course_coverage"] == "partial"
    sources_text = (output / "sources.md").read_text(encoding="utf-8")
    assert "Private lesson" in sources_text
    assert "Retired lesson" in sources_text
    assert "| failed |" in sources_text
    assert (tmp_path / ".agents/skills/contract-course").is_dir()


def test_build_skill_without_workspace_omits_unverified_ledger_and_warns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _contract_workspace(tmp_path)
    blueprint = _contract_blueprint(workspace)
    blueprint_path = tmp_path / "legacy-unverified.json"
    blueprint_path.write_text(blueprint.model_dump_json(indent=2), encoding="utf-8")
    output = tmp_path / "unverified-course"
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "build-skill",
            str(blueprint_path),
            "--host",
            "codex",
            "--project",
            "--output",
            str(output),
            "--skip-official",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    completion = json.loads(result.stdout)
    assert completion["workspace_verified"] is False
    assert completion["course_coverage"] == "unverified"
    assert "ledger was omitted" in completion["warnings"][0]
    assert "**Unverified:**" in (output / "sources.md").read_text(encoding="utf-8")
    provenance = json.loads((output / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["course_coverage"]["state"] == "unverified"


def test_build_skill_renders_validates_and_registers_for_second_invocation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evidence = {
        "source_id": "source-1",
        "start": 12,
        "end": 18,
        "modalities": ["speech", "visual"],
        "evidence_ids": ["transcript-1", "frame-1"],
    }
    artifacts = [
        CourseArtifact(
            id="foundations",
            path="chapters/foundations.md",
            title="Foundations",
            modes=["learn"],
            disclosure="normal",
            use_when="learning the demonstrated method",
            independent_loading_reason="Load the foundational lesson independently.",
            semantic_unit_ids=["unit-result"],
            content="# Foundations\n\nLearn the grounded method.\n",
        ),
        CourseArtifact(
            id="exercise",
            path="exercises/check-result.md",
            title="Check the result",
            modes=["practice"],
            disclosure="normal",
            use_when="practicing result verification",
            independent_loading_reason="Present practice without its rubric.",
            semantic_unit_ids=["unit-result"],
            content="# Exercise\n\nVerify an observable result.\n",
        ),
        CourseArtifact(
            id="solution",
            path="solutions/check-result.md",
            title="Check the result rubric",
            modes=["practice"],
            disclosure="after-attempt",
            use_when="reviewing an attempted verification",
            independent_loading_reason="Withhold the rubric until after an attempt.",
            semantic_unit_ids=["unit-result"],
            content="# Rubric\n\nRequire observable evidence.\n",
        ),
        CourseArtifact(
            id="application",
            path="playbooks/verify.md",
            title="Verify a result",
            modes=["apply"],
            disclosure="normal",
            use_when="applying the method",
            independent_loading_reason="Load the procedure for actual application.",
            semantic_unit_ids=["unit-result"],
            content="# Verify\n\n## Procedure\n\n1. Inspect the visible result.\n",
        ),
        CourseArtifact(
            id="reference",
            path="reference/evidence.md",
            title="Evidence reference",
            modes=["reference"],
            disclosure="normal",
            use_when="checking which evidence supports the result",
            independent_loading_reason="Load precise evidence rules without a lesson.",
            semantic_unit_ids=["unit-result"],
            content="# Evidence\n\nUse visual evidence for visible state.\n",
        ),
    ]
    artifact_claims = [
        CourseSkillClaim(
            id=f"claim-artifact-{index}",
            file=artifact.path,
            kind=("procedure-step" if "apply" in artifact.modes else "teaching-material"),
            summary=f"Ground {artifact.title.lower()} in the demonstrated result.",
            inferred="practice" in artifact.modes,
            confidence="medium" if "practice" in artifact.modes else "high",
            semantic_unit_ids=["unit-result"],
            evidence=[evidence],
        )
        for index, artifact in enumerate(artifacts, start=1)
    ]
    blueprint = CourseSkillBlueprint(
        name="grounded-course",
        title="Grounded Course",
        description=("Teach, practice, apply, and reference the demonstrated grounded workflow."),
        scope="Use the demonstrated workflow with explicit evidence checks.",
        artifact_language="English",
        interaction=CourseInteraction(
            welcome='Let us explore grounded verification. Say "start" and I will guide you.',
            starter_questions=["What visible result do you need to verify?"],
        ),
        capability_profile=CapabilityProfile(
            learn="strong",
            practice="strong",
            apply="strong",
            reference="strong",
            rationale="The source explains and demonstrates visible result verification.",
        ),
        curriculum=CurriculumDesign(
            selected_path_id="thematic",
            rationale="The thematic path links explanation, practice, and application.",
            paths=[
                CurriculumPath(
                    id="thematic",
                    title="Thematic course",
                    kind="thematic",
                    use_when="learning or applying grounded verification",
                    artifact_ids=["foundations", "exercise", "application", "reference"],
                )
            ],
        ),
        artifacts=artifacts,
        core_principles=[
            CorePrinciple(
                text="Verify the visible result.",
                claim_id="claim-core",
            )
        ],
        semantic_units=[
            SemanticUnit(
                id="unit-result",
                source_id="source-1",
                start=12,
                end=18,
                kind="claim",
                summary="Verify the visible result after the action.",
                materiality="core",
                disposition="included",
                inferred=False,
                confidence="high",
                modalities=["speech", "visual"],
                evidence_ids=["transcript-1", "frame-1"],
            )
        ],
        sources=[
            CourseSkillSource(
                id="source-1",
                title="Course",
                platform="youtube",
                url="https://youtu.be/course",
                coverage="complete",
            )
        ],
        claims=[
            CourseSkillClaim(
                id="claim-core",
                file="SKILL.md",
                kind="principle",
                summary="Verify the visible result after the action.",
                inferred=False,
                confidence="high",
                semantic_unit_ids=["unit-result"],
                evidence=[evidence],
            ),
            *artifact_claims,
        ],
    )
    workspace = Workspace.create(
        root=tmp_path / "private-workspace",
        inputs=["https://youtu.be/course"],
        settings=Settings(cache_root=tmp_path / "cache"),
    )
    workspace_source = SourceDescriptor(
        id="source-1",
        platform=SourcePlatform.YOUTUBE,
        locator="https://youtu.be/course",
        canonical_url="https://youtu.be/course",
        title="Course",
    )
    workspace.upsert_sources([workspace_source])
    workspace.replace_inspection_reports(
        [
            InspectionCompleteness(
                locator="https://youtu.be/course",
                platform=SourcePlatform.YOUTUBE,
                expected_entries=1,
                accessible_entries=1,
                inaccessible_entries=0,
                failed_entries=0,
                completeness_proven=True,
                entries=[
                    InspectionEntry(
                        ordinal=1,
                        status=InspectionEntryStatus.ACCESSIBLE,
                        source_id=workspace_source.id,
                        title=workspace_source.title,
                        locator=workspace_source.canonical_url,
                    )
                ],
            )
        ]
    )
    payload = blueprint.model_dump(mode="json")
    payload["sources"][0]["coverage"] = "partial"
    payload["coverage_ledger"] = coverage_ledger_from_workspace(workspace).model_dump(mode="json")
    blueprint = CourseSkillBlueprint.model_validate(payload)
    blueprint_path = tmp_path / "blueprint.json"
    blueprint_path.write_text(blueprint.model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "build-skill",
            str(blueprint_path),
            "--host",
            "codex",
            "--project",
            "--workspace",
            str(workspace.root),
            "--skip-official",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    portable = tmp_path / "generated-skills/grounded-course"
    installed = tmp_path / ".agents/skills/grounded-course"
    assert Path(payload["generated_path"]) == portable
    assert Path(payload["installed_path"]) == installed
    assert payload["valid"] is True
    assert payload["workspace_verified"] is True
    assert payload["course_coverage"] == "partial"
    assert portable.is_dir()
    assert installed.is_dir()
    resident = (installed / "SKILL.md").read_text(encoding="utf-8")
    assert "## Empty invocation" in resident
    assert "## Capability profile" in resident
    assert "**Learn:**" in resident
    assert "**Practice:**" in resident
    assert "**Apply:**" in resident
    assert "**Reference:**" in resident
    assert (installed / "source-map.md").is_file()
    assert (installed / "build-manifest.json").is_file()
