from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_author import _analyzed_workspace, _author_result, _curriculum_result
from test_review import _review_result

from video_to_skill.behavior import snapshot_behavior_target
from video_to_skill.config import Settings
from video_to_skill.coordinator import advance_run, submit_workspace_result
from video_to_skill.errors import ProcessingError
from video_to_skill.installation import SkillHost
from video_to_skill.models import ObservationProducer
from video_to_skill.orchestration import (
    BehaviorArtifactAccess,
    BehaviorTrialResult,
    BehaviorTurn,
    DecisionResult,
    RunEnvelope,
)
from video_to_skill.utils import atomic_write_json
from video_to_skill.work import WorkRole
from video_to_skill.workspace import Workspace


def _resume(
    workspace: Workspace,
    settings: Settings,
) -> RunEnvelope:
    return advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=None,
    )


def _submit_trial_envelope(
    workspace: Workspace,
    envelope: RunEnvelope,
    *,
    run_prefix: str,
) -> None:
    assert envelope.actions
    for index, action in enumerate(envelope.actions):
        task = workspace.get_work_item(action.task_id)
        assert task.scope["kind"] == "behavior-trial"
        lease = json.loads((action.task_path / "lease.json").read_text(encoding="utf-8"))
        packet = json.loads((workspace.root / task.packet_path).read_text(encoding="utf-8"))[
            "payload"
        ]
        target = snapshot_behavior_target(
            workspace.root / packet["evaluation_target"]["path"],
            expected_build_id=packet["evaluation_target"]["build_id"],
        )
        result_path = action.task_path / "output" / "result.json"
        atomic_write_json(
            result_path,
            BehaviorTrialResult(
                task_id=task.id,
                lease_token=str(lease["lease_token"]),
                execution_context_id=str(lease["execution_context_id"]),
                snapshot_digest=task.snapshot_digest,
                producer=ObservationProducer(
                    name=f"{run_prefix}-worker-{index}",
                    run_id=f"{run_prefix}-run-{index}",
                ),
                scenario_id=str(task.scope["scenario_id"]),
                scenario_digest=str(task.scope["scenario_digest"]),
                target_content_digest=target.content_digest,
                turns=[
                    BehaviorTurn(role="user", content=packet["scenario"]["prompt"]),
                    BehaviorTurn(role="assistant", content="A bounded grounded response."),
                ],
                artifact_accesses=[
                    BehaviorArtifactAccess(
                        path="SKILL.md",
                        sha256=str(target.files["SKILL.md"]["sha256"]),
                        reason="The host loaded the resident Skill instructions.",
                    )
                ],
            ),
        )
        submit_workspace_result(workspace, task.id, result_path)


def _submit_review_envelope(workspace: Workspace, envelope: RunEnvelope) -> None:
    [action] = envelope.actions
    task = workspace.get_work_item(action.task_id)
    assert task.scope["kind"] == "independent-review"
    lease = json.loads((action.task_path / "lease.json").read_text(encoding="utf-8"))
    result_path = action.task_path / "output" / "result.json"
    atomic_write_json(
        result_path,
        _review_result(workspace, task, str(lease["lease_token"])),
    )
    submit_workspace_result(workspace, task.id, result_path)


def _reviewed_workspace(
    tmp_path: Path,
) -> tuple[Workspace, Settings, Path, Path]:
    workspace, _analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)
    output = tmp_path / "generated" / "evidence-updated-conviction"
    skill_root = tmp_path / "skills"

    curriculum_envelope = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=SkillHost.CODEX,
        output=output,
        skill_root=skill_root,
        run_official_validation=False,
    )

    assert curriculum_envelope.status == "actions-required"
    [curriculum_action] = curriculum_envelope.actions
    assert curriculum_action.role == "author"
    curriculum_task = workspace.get_work_item(curriculum_action.task_id)
    assert curriculum_task.scope["kind"] == "curriculum-planning"
    curriculum_lease = json.loads(
        (curriculum_action.task_path / "lease.json").read_text(encoding="utf-8")
    )
    curriculum_result = _curriculum_result(
        curriculum_task,
        str(curriculum_lease["lease_token"]),
    )
    curriculum_result_path = curriculum_action.task_path / "output" / "result.json"
    atomic_write_json(curriculum_result_path, curriculum_result)
    submit_workspace_result(workspace, curriculum_task.id, curriculum_result_path)

    author_envelope = _resume(workspace, settings)
    [author_action] = author_envelope.actions
    assert author_action.role == "author"
    assert workspace.get_work_item(author_action.task_id).scope["kind"] == "course-authoring"
    assert "transcript" not in author_envelope.model_dump_json()
    author_task = workspace.get_work_item(author_action.task_id)
    lease = json.loads((author_action.task_path / "lease.json").read_text(encoding="utf-8"))
    draft = author_action.task_path / "output" / "course.md"
    draft.write_text(
        "# Evidence-Updated Conviction\n\nUse retrieval, practice, application, and reference.\n",
        encoding="utf-8",
    )
    author_result = _author_result(
        author_task,
        str(lease["lease_token"]),
        draft,
    )
    author_result_path = author_action.task_path / "output" / "result.json"
    atomic_write_json(author_result_path, author_result)
    author_receipt = submit_workspace_result(
        workspace,
        author_task.id,
        author_result_path,
    )
    assert author_receipt.status == "complete"

    trial_envelope = _resume(workspace, settings)
    assert all(action.role == "review" for action in trial_envelope.actions)
    for index, trial_action in enumerate(trial_envelope.actions):
        trial_task = workspace.get_work_item(trial_action.task_id)
        trial_lease = json.loads(
            (trial_action.task_path / "lease.json").read_text(encoding="utf-8")
        )
        packet = json.loads((workspace.root / trial_task.packet_path).read_text(encoding="utf-8"))[
            "payload"
        ]
        target = snapshot_behavior_target(
            workspace.root / packet["evaluation_target"]["path"],
            expected_build_id=packet["evaluation_target"]["build_id"],
        )
        trial_result_path = trial_action.task_path / "output" / "result.json"
        atomic_write_json(
            trial_result_path,
            BehaviorTrialResult(
                task_id=trial_task.id,
                lease_token=str(trial_lease["lease_token"]),
                execution_context_id=str(trial_lease["execution_context_id"]),
                snapshot_digest=trial_task.snapshot_digest,
                producer=ObservationProducer(
                    name=f"trial-worker-{index}",
                    run_id=f"trial-run-{index}",
                ),
                scenario_id=str(trial_task.scope["scenario_id"]),
                scenario_digest=str(trial_task.scope["scenario_digest"]),
                target_content_digest=target.content_digest,
                turns=[
                    BehaviorTurn(role="user", content=packet["scenario"]["prompt"]),
                    BehaviorTurn(role="assistant", content="A bounded grounded response."),
                ],
                artifact_accesses=[
                    BehaviorArtifactAccess(
                        path="SKILL.md",
                        sha256=str(target.files["SKILL.md"]["sha256"]),
                        reason="The host loaded the resident Skill instructions.",
                    )
                ],
            ),
        )
        submit_workspace_result(workspace, trial_task.id, trial_result_path)

    review_envelope = _resume(workspace, settings)
    [review_action] = review_envelope.actions
    assert review_action.role == "review"
    review_task = workspace.get_work_item(review_action.task_id)
    review_lease = json.loads((review_action.task_path / "lease.json").read_text(encoding="utf-8"))
    review_result = _review_result(
        workspace,
        review_task,
        str(review_lease["lease_token"]),
    )
    review_result_path = review_action.task_path / "output" / "result.json"
    atomic_write_json(review_result_path, review_result)
    review_receipt = submit_workspace_result(
        workspace,
        review_task.id,
        review_result_path,
    )
    assert review_receipt.status == "complete"
    return workspace, settings, output, skill_root


def test_run_and_submit_complete_without_main_agent_data_forwarding(
    tmp_path: Path,
) -> None:
    workspace, settings, _output, _skill_root = _reviewed_workspace(tmp_path)

    complete = _resume(workspace, settings)

    assert complete.status == "complete"
    assert complete.completion is not None
    assert Path(str(complete.completion["generated_path"])).is_dir()
    assert Path(str(complete.completion["installed_path"])).is_dir()
    assert complete.completion["instructional_affordance_coverage"]["provided"] == 5
    resumed = _resume(workspace, settings)
    assert resumed.status == "complete"
    assert resumed.completion is not None
    assert resumed.completion["build_id"] == complete.completion["build_id"]
    assert resumed.completion["generated_path"] == complete.completion["generated_path"]
    assert resumed.completion["installed_path"] == complete.completion["installed_path"]
    assert resumed.completion["installation_status"] == "unchanged"


def test_material_curriculum_decision_precedes_artifact_authoring(tmp_path: Path) -> None:
    workspace, _analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)
    first = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "evidence-updated-conviction",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
    )
    [curriculum_action] = first.actions
    curriculum_task = workspace.get_work_item(curriculum_action.task_id)
    curriculum_lease = json.loads(
        (curriculum_action.task_path / "lease.json").read_text(encoding="utf-8")
    )
    curriculum_result_path = curriculum_action.task_path / "output" / "result.json"
    atomic_write_json(
        curriculum_result_path,
        _curriculum_result(
            curriculum_task,
            str(curriculum_lease["lease_token"]),
            decision_required=True,
        ),
    )
    submit_workspace_result(workspace, curriculum_task.id, curriculum_result_path)

    decision_envelope = _resume(workspace, settings)
    [decision_action] = decision_envelope.actions
    assert decision_action.kind == "ask-user"
    assert all(
        workspace.canonical_record(kind) is None
        for kind in ("course", "artifact-plan", "claims", "assets")
    )
    assert not any(
        item.scope.get("kind") == "course-authoring"
        for item in workspace.list_work_items(curriculum_task.run_id)
    )
    repeated = _resume(workspace, settings)
    [repeated_action] = repeated.actions
    assert repeated_action.task_id == decision_action.task_id
    assert repeated_action.already_leased is True

    decision_task = workspace.get_work_item(decision_action.task_id)
    decision_lease = json.loads(
        (decision_action.task_path / "lease.json").read_text(encoding="utf-8")
    )
    decision_result_path = decision_action.task_path / "output" / "result.json"
    atomic_write_json(
        decision_result_path,
        DecisionResult(
            task_id=decision_task.id,
            lease_token=str(decision_lease["lease_token"]),
            snapshot_digest=decision_task.snapshot_digest,
            producer=ObservationProducer(name="user"),
            selected_option_id="application-first",
        ),
    )
    submit_workspace_result(workspace, decision_task.id, decision_result_path)

    author_envelope = _resume(workspace, settings)
    [author_action] = author_envelope.actions
    author_task = workspace.get_work_item(author_action.task_id)
    options_record = workspace.canonical_record("curriculum-options")
    selection_record = workspace.canonical_record("selected-curriculum")
    assert options_record is not None
    assert selection_record is not None
    selection = json.loads((workspace.root / selection_record.path).read_text(encoding="utf-8"))
    assert selection["selected_path_id"] == "application-first"
    assert selection["source"] == "user"
    assert author_task.scope["curriculum_plan_digest"] == options_record.digest
    assert author_task.scope["curriculum_selection_digest"] == selection_record.digest
    assert author_task.scope["curriculum_selection_producer_task_id"] == decision_task.id
    assert set(author_task.dependencies) == {
        str(curriculum_task.scope["analyze_task_id"]),
        curriculum_task.id,
        decision_task.id,
    }


def test_run_revalidates_cached_completion_and_recovers_missing_artifacts(
    tmp_path: Path,
) -> None:
    workspace, settings, _output, _skill_root = _reviewed_workspace(tmp_path)
    complete = _resume(workspace, settings)
    assert complete.completion is not None
    generated = Path(str(complete.completion["generated_path"]))
    installed = Path(str(complete.completion["installed_path"]))
    validation_report = Path(str(complete.completion["validation_report_path"]))
    completion_path = (
        workspace.root / "builds" / str(complete.completion["build_id"]) / "completion.json"
    )

    shutil.rmtree(generated)
    shutil.rmtree(installed)
    validation_report.unlink()
    atomic_write_json(completion_path, {"generated_path": "/stale/generated/path"})

    recovered = _resume(workspace, settings)

    assert recovered.status == "complete"
    assert recovered.completion is not None
    assert recovered.completion["generated_path"] == str(generated)
    assert recovered.completion["installed_path"] == str(installed)
    assert generated.is_dir()
    assert installed.is_dir()
    assert validation_report.is_file()
    assert json.loads(completion_path.read_text(encoding="utf-8")) == recovered.completion

    (installed / "unexpected.txt").write_text("drift", encoding="utf-8")
    drift = _resume(workspace, settings)
    assert drift.status == "actions-required"
    [action] = drift.actions
    assert action.role == "decision"
    task = workspace.get_work_item(action.task_id)
    packet = json.loads((workspace.root / task.packet_path).read_text(encoding="utf-8"))
    assert any("installed Skill" in conflict for conflict in packet["payload"]["conflicts"])


def test_run_recovers_from_generated_and_installed_name_conflicts(
    tmp_path: Path,
) -> None:
    workspace, settings, output, skill_root = _reviewed_workspace(tmp_path)
    output.mkdir(parents=True)
    (output / "different.txt").write_text("different generated build", encoding="utf-8")
    installed_conflict = skill_root / "evidence-updated-conviction"
    installed_conflict.mkdir(parents=True)
    (installed_conflict / "SKILL.md").write_text("different installed Skill", encoding="utf-8")

    decision_envelope = _resume(workspace, settings)

    [action] = decision_envelope.actions
    assert action.kind == "ask-user"
    assert action.role == "decision"
    assert len(action.options) == 3
    repeated = _resume(workspace, settings)
    [repeated_action] = repeated.actions
    assert repeated_action.task_id == action.task_id
    assert repeated_action.already_leased is True

    task = workspace.get_work_item(action.task_id)
    lease = json.loads((action.task_path / "lease.json").read_text(encoding="utf-8"))
    selected = action.options[0]
    result = DecisionResult(
        task_id=task.id,
        lease_token=str(lease["lease_token"]),
        snapshot_digest=task.snapshot_digest,
        producer=ObservationProducer(name="user"),
        selected_option_id=selected["id"],
    )
    result_path = action.task_path / "output" / "result.json"
    atomic_write_json(result_path, result)
    submit_workspace_result(workspace, task.id, result_path)

    fresh_trials = _resume(workspace, settings)
    assert fresh_trials.status == "actions-required"
    _submit_trial_envelope(workspace, fresh_trials, run_prefix="renamed-trial")
    fresh_review = _resume(workspace, settings)
    _submit_review_envelope(workspace, fresh_review)
    complete = _resume(workspace, settings)

    assert complete.status == "complete"
    assert complete.completion is not None
    assert complete.completion["name"] == selected["id"]
    assert Path(str(complete.completion["generated_path"])) == Path(selected["output"])
    assert Path(str(complete.completion["installed_path"])).name == selected["id"]
    assert (output / "different.txt").read_text(encoding="utf-8") == "different generated build"
    assert (installed_conflict / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "different installed Skill"
    assert workspace.canonical_record("delivery-selection") is not None


def test_run_reclaims_expired_task_lease(tmp_path: Path) -> None:
    workspace, _analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)
    first = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "evidence-updated-conviction",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
    )
    [first_action] = first.actions
    first_attempts = workspace.get_work_item(first_action.task_id).attempt_count
    with workspace.connect() as connection:
        connection.execute(
            "UPDATE work_items SET lease_expires_at=? WHERE id=?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                first_action.task_id,
            ),
        )

    resumed = _resume(workspace, settings)

    [resumed_action] = resumed.actions
    assert resumed_action.task_id == first_action.task_id
    assert resumed_action.already_leased is False
    assert workspace.get_work_item(first_action.task_id).attempt_count == first_attempts + 1


def test_run_resumes_pre_checkpoint_author_task_without_replanning(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    legacy_author = workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope={"kind": "course-authoring", "revision": 1},
        persona_hint="Legacy course Author.",
        packet={},
        result_schema={"type": "object"},
        dependencies=[analyze_task.id],
        snapshot_digest=run.snapshot_digest,
    )

    resumed = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=Settings(cache_root=tmp_path),
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "legacy-course",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
    )

    [action] = resumed.actions
    assert action.task_id == legacy_author.id
    assert not any(
        item.scope.get("kind") == "curriculum-planning"
        for item in workspace.list_work_items(run.id)
    )


def test_submit_rejects_result_outside_task_output_before_parsing(
    tmp_path: Path,
) -> None:
    workspace, _analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)
    envelope = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "evidence-updated-conviction",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
    )
    [action] = envelope.actions
    outside = tmp_path / "not-even-json.txt"
    outside.write_text("not JSON", encoding="utf-8")

    with pytest.raises(ProcessingError, match="regular task-output JSON"):
        submit_workspace_result(workspace, action.task_id, outside)


def test_submit_rejects_symlinked_result_before_parsing(tmp_path: Path) -> None:
    workspace, _analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)
    envelope = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "evidence-updated-conviction",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
    )
    [action] = envelope.actions
    target = action.task_path / "output" / "target.txt"
    target.write_text("not JSON", encoding="utf-8")
    symlink = action.task_path / "output" / "result.json"
    symlink.symlink_to(target)

    with pytest.raises(ProcessingError, match="regular task-output JSON"):
        submit_workspace_result(workspace, action.task_id, symlink)
