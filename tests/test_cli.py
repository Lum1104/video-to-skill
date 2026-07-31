import json
from pathlib import Path

from typer.testing import CliRunner

from video_to_skill.cli import app
from video_to_skill.config import Settings
from video_to_skill.generation import (
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


def test_cli_exposes_workspace_protocol_without_legacy_authoring_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "run" in result.output
    assert "submit" in result.output
    assert "blueprint-schema" not in result.output
    assert "build-skill" not in result.output
    assert result.stdout.strip()


def test_run_and_extract_explain_distinct_source_and_output_languages() -> None:
    for command in ("run", "extract"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.output
        assert "--output-language" in result.output
        assert "caption/ASR" in result.output
        assert "artifact language" in result.output


def test_cli_rejects_non_concrete_output_language(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["run", "--workspace", str(tmp_path / "missing"), "--output-language", "und"],
    )

    assert result.exit_code == 2
    assert "concrete language or locale" in result.output


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
