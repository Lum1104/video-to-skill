from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from video_to_skill.analyze import plan_analyze_tasks, submit_analyze_result
from video_to_skill.author import plan_author_task, submit_author_result
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import (
    CapabilityProfile,
    CourseInteraction,
    CourseSkillClaim,
    CurriculumDesign,
    CurriculumPath,
    SemanticUnit,
)
from video_to_skill.models import (
    ObservationProducer,
    SemanticSegment,
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
)
from video_to_skill.orchestration import (
    AFFORDANCE_CATALOG,
    AnalyzeResult,
    ArtifactDraftSpec,
    AuthorResult,
    CapabilityEvidence,
    InstructionalAffordance,
    SemanticCoverage,
)
from video_to_skill.utils import atomic_write_json, hash_file
from video_to_skill.work import WorkItem, WorkState
from video_to_skill.workspace import Workspace


def _analyzed_workspace(tmp_path: Path) -> tuple[Workspace, WorkItem]:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    source = SourceDescriptor(
        id="source",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/demo.mp4",
        title="Founder interview",
        duration=120,
    )
    transcript = TranscriptSegment(
        id="transcript-1",
        source_id=source.id,
        start=0,
        end=60,
        text="Test a conviction with evidence and update it when reality changes.",
        origin=TranscriptOrigin.MANUAL_CAPTION,
    )
    section = SemanticSegment(
        id="section-1",
        source_id=source.id,
        ordinal=1,
        title="Evidence-updated conviction",
        start=0,
        end=120,
        transcript_ids=[transcript.id],
    )
    workspace.upsert_sources([source])
    workspace.replace_transcripts(source.id, [transcript])
    workspace.replace_semantic_segments(source.id, [section])
    run = workspace.create_analysis_run()
    [task] = plan_analyze_tasks(workspace, run)
    lease = workspace.lease_work_item(task.id, owner="codex")
    unit = SemanticUnit(
        id="unit-conviction",
        source_id=source.id,
        start=0,
        end=60,
        kind="recommendation",
        summary="Update conviction when evidence changes.",
        materiality="core",
        disposition="included",
        inferred=False,
        confidence="high",
        modalities=["speech"],
        evidence_ids=[transcript.id],
    )
    result = AnalyzeResult(
        task_id=task.id,
        lease_token=lease.token,
        snapshot_digest=task.snapshot_digest,
        producer=ObservationProducer(name="analysis-worker"),
        integrated=True,
        semantic_units=[unit],
        capability_evidence=[
            CapabilityEvidence(
                mode=mode,
                ceiling="strong",
                semantic_unit_ids=[unit.id],
                rationale="The source supports instruction and transfer.",
            )
            for mode in ("learn", "practice", "apply", "reference")
        ],
        coverage=SemanticCoverage(
            source_ids=[source.id],
            core_units=1,
            supporting_units=0,
            contextual_units=0,
            incidental_units=0,
            included_units=1,
            merged_units=0,
            context_only_units=0,
            omitted_units=0,
            material_units_accounted_for=True,
        ),
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)
    analyze_task = submit_analyze_result(workspace, task.id, result_path)
    return workspace, analyze_task


def _affordances(*, strong_practice: bool = False) -> list[InstructionalAffordance]:
    provided = {
        ("learn", "retrieval-prompts"),
        ("practice", "focused-exercises"),
        ("practice", "success-criteria"),
        ("apply", "operational-playbook"),
        ("reference", "quick-reference"),
    }
    if strong_practice:
        provided |= {
            ("practice", "scored-rubric"),
            ("practice", "progressive-hints"),
            ("practice", "retry-loop"),
        }
    result: list[InstructionalAffordance] = []
    for mode, kinds in AFFORDANCE_CATALOG.items():
        for kind in kinds:
            is_provided = (mode, kind) in provided
            result.append(
                InstructionalAffordance(
                    id=f"affordance-{mode}-{kind}",
                    mode=mode,
                    kind=kind,
                    status="provided" if is_provided else "unsupported",
                    artifact_ids=["artifact-course"] if is_provided else [],
                    semantic_unit_ids=["unit-conviction"],
                    rationale=(
                        "The artifact provides this learning surface."
                        if is_provided
                        else "The light capability does not claim this surface."
                    ),
                )
            )
    return result


def _author_result(
    task: WorkItem,
    lease_token: str,
    draft: Path,
    *,
    practice_level: str = "light",
    strong_practice_ledger: bool = False,
) -> AuthorResult:
    affordances = _affordances(strong_practice=strong_practice_ledger)
    provided_ids = [item.id for item in affordances if item.status == "provided"]
    artifact = ArtifactDraftSpec(
        id="artifact-course",
        path="chapters/evidence-updated-conviction.md",
        title="Evidence-Updated Conviction",
        modes=["learn", "practice", "apply", "reference"],
        disclosure="normal",
        use_when="learning or applying the source's conviction loop",
        independent_loading_reason="Load the complete decision loop as one coherent guide.",
        semantic_unit_ids=["unit-conviction"],
        affordance_ids=provided_ids,
        topics=["conviction", "evidence"],
        draft_path=draft.name,
        draft_sha256=hash_file(draft),
    )
    return AuthorResult(
        task_id=task.id,
        lease_token=lease_token,
        snapshot_digest=task.snapshot_digest,
        producer=ObservationProducer(name="author-worker"),
        name="evidence-updated-conviction",
        title="Evidence-Updated Conviction",
        description="Teach and apply evidence-updated conviction from the grounded source.",
        scope="Use evidence to form, test, and update a founder thesis.",
        artifact_language="English",
        interaction=CourseInteraction(
            welcome='Build conviction without becoming rigid—or say "start".',
            starter_questions=["What belief are you testing?"],
        ),
        capability_profile=CapabilityProfile(
            learn="light",
            practice=practice_level,
            apply="light",
            reference="light",
            rationale="The source supports a compact transferable decision loop.",
        ),
        curriculum=CurriculumDesign(
            selected_path_id="thematic",
            rationale="The thematic path connects the principle to a decision loop.",
            paths=[
                CurriculumPath(
                    id="thematic",
                    title="Evidence-Updated Conviction",
                    kind="thematic",
                    use_when="learning and applying the complete loop",
                    artifact_ids=[artifact.id],
                )
            ],
        ),
        artifacts=[artifact],
        affordance_ledger=affordances,
        claims=[
            CourseSkillClaim(
                id="claim-conviction",
                file=artifact.path,
                kind="principle",
                summary="Conviction should update when evidence changes.",
                inferred=False,
                confidence="high",
                semantic_unit_ids=["unit-conviction"],
                evidence=[
                    {
                        "source_id": "source",
                        "start": 0,
                        "end": 60,
                        "modalities": ["speech"],
                        "evidence_ids": ["transcript-1"],
                    }
                ],
            )
        ],
    )


def test_author_task_persists_affordance_ledger_and_draft(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = plan_author_task(workspace, run, analyze_task=analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_text(
        "# Evidence-Updated Conviction\n\nUse a retrieval prompt, exercise, decision loop, and quick reference.\n",
        encoding="utf-8",
    )
    result = _author_result(task, lease.token, draft)
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    accepted = submit_author_result(workspace, task.id, result_path)

    assert accepted.state == WorkState.COMPLETE
    assert workspace.canonical_record("instructional-affordances") is not None
    canonical_draft = workspace.canonical_record("artifact-draft", "artifact-course")
    assert canonical_draft is not None
    assert (workspace.root / canonical_draft.path).read_text(encoding="utf-8").startswith(
        "# Evidence-Updated Conviction"
    )


def test_strong_practice_requires_capstone_affordance(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = plan_author_task(workspace, run, analyze_task=analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_text("# Course\n", encoding="utf-8")

    with pytest.raises(PydanticValidationError, match="capstone"):
        _author_result(
            task,
            lease.token,
            draft,
            practice_level="strong",
            strong_practice_ledger=True,
        )


def test_author_submission_rejects_changed_draft(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = plan_author_task(workspace, run, analyze_task=analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_text("# Course\n", encoding="utf-8")
    result = _author_result(task, lease.token, draft)
    draft.write_text("# Tampered Course\n", encoding="utf-8")
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    with pytest.raises(ProcessingError, match="digest does not match"):
        submit_author_result(workspace, task.id, result_path)
