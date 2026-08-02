from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError as PydanticValidationError

from video_to_skill.analyze import plan_analyze_tasks, submit_analyze_result
from video_to_skill.author import (
    MAX_AUTHOR_DRAFT_BYTES,
    plan_author_task,
    submit_author_result,
)
from video_to_skill.config import Settings
from video_to_skill.curriculum import plan_curriculum_task, submit_curriculum_plan_result
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import (
    CapabilityProfile,
    CourseInteraction,
    CourseSkillClaim,
    CurriculumDesign,
    CurriculumPath,
    SemanticUnit,
    VisualAssetCandidate,
)
from video_to_skill.models import (
    ObservationProducer,
    SemanticSegment,
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
    VisualEvent,
)
from video_to_skill.orchestration import (
    AFFORDANCE_CATALOG,
    AnalyzeResult,
    ArtifactDraftSpec,
    AuthorResult,
    AuthorVisualAsset,
    CapabilityEvidence,
    CurriculumPlan,
    CurriculumPlanPath,
    CurriculumPlanResult,
    InstructionalAffordance,
    SemanticCoverage,
)
from video_to_skill.utils import atomic_write_json, hash_file
from video_to_skill.work import AnalysisRun, WorkItem, WorkState
from video_to_skill.workspace import Workspace


def _analyzed_workspace(
    tmp_path: Path,
    *,
    with_visual: bool = False,
) -> tuple[Workspace, WorkItem]:
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
    visual = None
    if with_visual:
        frame_path = workspace.root / "frames" / "status.png"
        frame_path.parent.mkdir()
        Image.new("RGB", (120, 80), "#2d5aa6").save(frame_path)
        visual = VisualEvent(
            id="frame-status",
            source_id=source.id,
            timestamp=30,
            path=frame_path,
        )
    section = SemanticSegment(
        id="section-1",
        source_id=source.id,
        ordinal=1,
        title="Evidence-updated conviction",
        start=0,
        end=120,
        transcript_ids=[transcript.id],
        visual_event_ids=[visual.id] if visual is not None else [],
    )
    workspace.upsert_sources([source])
    workspace.replace_transcripts(source.id, [transcript])
    if visual is not None:
        workspace.upsert_visuals([visual])
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
        modalities=["speech", "visual"] if visual is not None else ["speech"],
        evidence_ids=[transcript.id, visual.id] if visual is not None else [transcript.id],
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
        visual_asset_candidates=(
            [
                VisualAssetCandidate(
                    id="status-panel",
                    source_id=source.id,
                    evidence_ids=[visual.id],
                    semantic_unit_ids=[unit.id],
                    presentation="frame",
                    description="The visible status after the decision",
                    teaching_value="The interface state verifies the spoken principle.",
                )
            ]
            if visual is not None
            else []
        ),
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
    with_visual: bool = False,
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
        assets=(
            [
                AuthorVisualAsset(
                    candidate_id="status-panel",
                    path="assets/status-panel.png",
                    description="The visible status after the decision",
                    used_by=[artifact.path],
                    claim_ids=["claim-conviction"],
                )
            ]
            if with_visual
            else []
        ),
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
                        "modalities": (["speech", "visual"] if with_visual else ["speech"]),
                        "evidence_ids": (
                            ["transcript-1", "frame-status"] if with_visual else ["transcript-1"]
                        ),
                    }
                ],
            )
        ],
    )


def _curriculum_result(
    task: WorkItem,
    lease_token: str,
    *,
    decision_required: bool = False,
    artifact_language: str = "English",
) -> CurriculumPlanResult:
    paths = [
        CurriculumPlanPath(
            id="thematic",
            title="Evidence-Updated Conviction",
            kind="thematic",
            use_when="learning and applying the complete loop",
            unit_sequence=["unit-conviction"],
        )
    ]
    if decision_required:
        paths.append(
            CurriculumPlanPath(
                id="application-first",
                title="Conviction in Practice",
                kind="application-first",
                use_when="starting from a live decision",
                unit_sequence=["unit-conviction"],
            )
        )
    return CurriculumPlanResult(
        task_id=task.id,
        lease_token=lease_token,
        snapshot_digest=task.snapshot_digest,
        producer=ObservationProducer(name="curriculum-worker", run_id="curriculum-run"),
        artifact_language=artifact_language,
        curriculum=CurriculumPlan(
            recommended_path_id="thematic",
            rationale="The thematic path connects the principle to a decision loop.",
            paths=paths,
            decision_required=decision_required,
            decision_summary=(
                "Choose a thematic course or an application-first learning experience."
                if decision_required
                else None
            ),
        ),
    )


def _plan_course_author_task(
    workspace: Workspace,
    run: AnalysisRun,
    analyze_task: WorkItem,
) -> WorkItem:
    curriculum_task = plan_curriculum_task(workspace, run, analyze_task=analyze_task)
    curriculum_lease = workspace.lease_work_item(curriculum_task.id, owner="codex")
    curriculum_result = _curriculum_result(curriculum_task, curriculum_lease.token)
    curriculum_result_path = curriculum_lease.output_directory / "result.json"
    atomic_write_json(curriculum_result_path, curriculum_result)
    accepted_curriculum = submit_curriculum_plan_result(
        workspace,
        curriculum_task.id,
        curriculum_result_path,
    )
    return plan_author_task(
        workspace,
        run,
        analyze_task=analyze_task,
        curriculum_task=accepted_curriculum,
    )


def test_author_task_persists_affordance_ledger_and_draft(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = _plan_course_author_task(workspace, run, analyze_task)
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
    assert (
        (workspace.root / canonical_draft.path)
        .read_text(encoding="utf-8")
        .startswith("# Evidence-Updated Conviction")
    )


def test_author_accepts_the_validated_draft_snapshot_if_path_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = _plan_course_author_task(workspace, run, analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    original_payload = b"# Original Course\n\nGrounded content.\n"
    draft.write_bytes(original_payload)
    result = _author_result(task, lease.token, draft)
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)
    original_accept = Workspace.accept_work_result

    def replace_before_accept(self: Workspace, **kwargs):
        draft.write_text("# Replacement Course\n", encoding="utf-8")
        return original_accept(self, **kwargs)

    monkeypatch.setattr(Workspace, "accept_work_result", replace_before_accept)

    accepted = submit_author_result(workspace, task.id, result_path)

    assert accepted.state == WorkState.COMPLETE
    canonical_draft = workspace.canonical_record("artifact-draft", "artifact-course")
    assert canonical_draft is not None
    assert canonical_draft.digest == result.artifacts[0].draft_sha256
    assert canonical_draft.digest == sha256(original_payload).hexdigest()
    assert (workspace.root / canonical_draft.path).read_bytes() == original_payload
    assert draft.read_text(encoding="utf-8") == "# Replacement Course\n"


def test_author_rejects_symlinked_draft(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = _plan_course_author_task(workspace, run, analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    target = tmp_path / "external-course.md"
    target.write_text("# External Course\n", encoding="utf-8")
    draft = lease.output_directory / "course.md"
    draft.symlink_to(target)
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, _author_result(task, lease.token, draft))

    with pytest.raises(ProcessingError, match="unsafe"):
        submit_author_result(workspace, task.id, result_path)


def test_author_rejects_oversized_draft(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = _plan_course_author_task(workspace, run, analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_bytes(b"x" * (MAX_AUTHOR_DRAFT_BYTES + 1))
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, _author_result(task, lease.token, draft))

    with pytest.raises(ProcessingError, match="exceeds its size limit"):
        submit_author_result(workspace, task.id, result_path)


def test_author_rejects_non_utf8_draft(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = _plan_course_author_task(workspace, run, analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_bytes(b"\xff\xfe\x00")
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, _author_result(task, lease.token, draft))

    with pytest.raises(ProcessingError, match="Could not read artifact"):
        submit_author_result(workspace, task.id, result_path)


def test_author_cannot_drift_from_curriculum_artifact_language(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = _plan_course_author_task(workspace, run, analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_text("# Course\n\nGrounded content.\n", encoding="utf-8")
    result = _author_result(task, lease.token, draft).model_copy(
        update={"artifact_language": "French"}
    )
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    with pytest.raises(ProcessingError, match="changed the canonical"):
        submit_author_result(workspace, task.id, result_path)


def test_author_selects_only_materialized_visuals_linked_by_drafts(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path, with_visual=True)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = _plan_course_author_task(workspace, run, analyze_task)
    packet = json.loads((workspace.root / task.packet_path).read_text(encoding="utf-8"))["payload"]
    assert packet["visual_asset_candidates"][0]["candidate_id"] == "status-panel"
    candidate_path = workspace.root / packet["visual_asset_candidates"][0]["image_path"]
    assert candidate_path.is_file()

    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_text(
        "# Evidence-Updated Conviction\n\n![Decision status](../assets/status-panel.png)\n",
        encoding="utf-8",
    )
    result = _author_result(task, lease.token, draft, with_visual=True)
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    accepted = submit_author_result(workspace, task.id, result_path)

    assert accepted.state == WorkState.COMPLETE
    assets_record = workspace.canonical_record("assets")
    assert assets_record is not None
    assets = json.loads((workspace.root / assets_record.path).read_text(encoding="utf-8"))
    assert assets[0]["candidate_id"] == "status-panel"
    assert assets[0]["evidence_ids"] == ["frame-status"]
    assert assets[0]["source_path"].endswith(".png")
    assert len(assets[0]["source_sha256"]) == 64


def test_author_rejects_selected_visual_missing_from_draft(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path, with_visual=True)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = _plan_course_author_task(workspace, run, analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_text("# Evidence-Updated Conviction\n", encoding="utf-8")
    result = _author_result(task, lease.token, draft, with_visual=True)
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    with pytest.raises(ProcessingError, match="does not link selected visual asset"):
        submit_author_result(workspace, task.id, result_path)


def test_strong_practice_requires_capstone_affordance(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = _plan_course_author_task(workspace, run, analyze_task)
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
    task = _plan_course_author_task(workspace, run, analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_text("# Course\n", encoding="utf-8")
    result = _author_result(task, lease.token, draft)
    draft.write_text("# Tampered Course\n", encoding="utf-8")
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, result)

    with pytest.raises(ProcessingError, match="digest does not match"):
        submit_author_result(workspace, task.id, result_path)


def test_author_submission_rejects_claim_evidence_outside_semantic_units(
    tmp_path: Path,
) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = _plan_course_author_task(workspace, run, analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_text("# Course\n", encoding="utf-8")
    payload = _author_result(task, lease.token, draft).model_dump(mode="json")
    payload["claims"][0]["evidence"][0]["evidence_ids"] = ["invented-evidence"]
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, payload)

    with pytest.raises(ProcessingError, match="outside its semantic units"):
        submit_author_result(workspace, task.id, result_path)


def test_author_submission_rejects_canonical_curriculum_drift(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = _plan_course_author_task(workspace, run, analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    draft = lease.output_directory / "course.md"
    draft.write_text("# Course\n", encoding="utf-8")
    payload = _author_result(task, lease.token, draft).model_dump(mode="json")
    payload["curriculum"]["paths"][0]["title"] = "A redesigned path"
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, payload)

    with pytest.raises(ProcessingError, match="changed canonical path metadata"):
        submit_author_result(workspace, task.id, result_path)


def test_author_artifact_paths_match_portable_renderer_contract(tmp_path: Path) -> None:
    draft = tmp_path / "course.md"
    draft.write_text("# Course\n", encoding="utf-8")
    with pytest.raises(PydanticValidationError, match="root artifact path"):
        ArtifactDraftSpec(
            id="artifact-course",
            path="course.md",
            title="Course",
            modes=["learn"],
            disclosure="normal",
            use_when="learning the course",
            independent_loading_reason="Load the course as one coherent guide.",
            semantic_unit_ids=["unit-conviction"],
            draft_path=draft.name,
            draft_sha256=hash_file(draft),
        )
