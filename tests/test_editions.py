from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_author import _analyzed_workspace, _author_result, _curriculum_result
from test_coordinator import _submit_review_envelope, _submit_trial_envelope
from test_review import _review_result

from video_to_skill.config import Settings
from video_to_skill.coordinator import advance_run, submit_workspace_result
from video_to_skill.editions import load_edition_state
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import CurriculumDesign, CurriculumPath
from video_to_skill.installation import SkillHost
from video_to_skill.models import ObservationProducer
from video_to_skill.orchestration import DecisionResult
from video_to_skill.utils import atomic_write_json
from video_to_skill.work import WorkRole
from video_to_skill.workspace import Workspace


def _resume_edition(workspace: Workspace, settings: Settings, name: str):
    return advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=None,
        edition_name=name,
    )


def _workspace_with_material_curriculum(
    tmp_path: Path,
) -> tuple[Workspace, Settings, str]:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)
    first = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=SkillHost.CODEX,
        output=tmp_path / "legacy-output",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
    )
    [action] = first.actions
    task = workspace.get_work_item(action.task_id)
    lease = json.loads((action.task_path / "lease.json").read_text(encoding="utf-8"))
    result_path = action.task_path / "output" / "result.json"
    atomic_write_json(
        result_path,
        _curriculum_result(
            task,
            str(lease["lease_token"]),
            decision_required=True,
        ),
    )
    submit_workspace_result(workspace, task.id, result_path)

    decision = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=None,
    )
    [decision_action] = decision.actions
    decision_task = workspace.get_work_item(decision_action.task_id)
    decision_lease = json.loads(
        (decision_action.task_path / "lease.json").read_text(encoding="utf-8")
    )
    decision_path = decision_action.task_path / "output" / "result.json"
    atomic_write_json(
        decision_path,
        DecisionResult(
            task_id=decision_task.id,
            lease_token=str(decision_lease["lease_token"]),
            snapshot_digest=decision_task.snapshot_digest,
            producer=ObservationProducer(name="user"),
            selected_option_id="thematic",
        ),
    )
    submit_workspace_result(workspace, decision_task.id, decision_path)
    return workspace, settings, analyze_task.id


def _edition_view(workspace: Workspace, name: str) -> Workspace:
    state = load_edition_state(workspace, name)
    return workspace.for_edition(state.configuration.edition_id)


def _edition_author_result(
    task,
    lease_token: str,
    draft: Path,
    *,
    selected_path_id: str,
    artifact_language: str,
    skill_name: str,
):
    base = _author_result(task, lease_token, draft)
    artifact_id = base.artifacts[0].id
    localized = artifact_language == "Chinese"
    return base.model_copy(
        update={
            "name": skill_name,
            "artifact_language": artifact_language,
            "curriculum": CurriculumDesign(
                selected_path_id=selected_path_id,
                rationale=(
                    "主题路径把原则连接到决策循环。"
                    if localized
                    else "The thematic path connects the principle to a decision loop."
                ),
                paths=[
                    CurriculumPath(
                        id="thematic",
                        title="证据更新的信念" if localized else "Evidence-Updated Conviction",
                        kind="thematic",
                        use_when=(
                            "学习并应用完整循环"
                            if localized
                            else "learning and applying the complete loop"
                        ),
                        artifact_ids=[artifact_id],
                    ),
                    CurriculumPath(
                        id="application-first",
                        title="实践中的信念" if localized else "Conviction in Practice",
                        kind="application-first",
                        use_when=(
                            "从真实决策开始" if localized else "starting from a live decision"
                        ),
                        artifact_ids=[artifact_id],
                    ),
                ],
            ),
        }
    )


def _submit_edition_author(
    base: Workspace,
    view: Workspace,
    envelope,
    *,
    selected_path_id: str,
    artifact_language: str,
    skill_name: str,
) -> None:
    [action] = envelope.actions
    task = view.get_work_item(action.task_id)
    lease = json.loads((action.task_path / "lease.json").read_text(encoding="utf-8"))
    draft = action.task_path / "output" / "course.md"
    draft.write_text(
        "# Evidence-Updated Conviction\n\nUse grounded retrieval and application.\n",
        encoding="utf-8",
    )
    result_path = action.task_path / "output" / "result.json"
    atomic_write_json(
        result_path,
        _edition_author_result(
            task,
            str(lease["lease_token"]),
            draft,
            selected_path_id=selected_path_id,
            artifact_language=artifact_language,
            skill_name=skill_name,
        ),
    )
    # Submission starts from the unscoped workspace and resolves the immutable
    # edition directly from the task scope; no ambient active-edition switch exists.
    submit_workspace_result(base, task.id, result_path)


def _finish_passing_edition(
    base: Workspace,
    view: Workspace,
    settings: Settings,
    name: str,
):
    trials = _resume_edition(base, settings, name)
    _submit_trial_envelope(view, trials, run_prefix=name)
    review = _resume_edition(base, settings, name)
    _submit_review_envelope(view, review)
    return _resume_edition(base, settings, name)


def test_two_named_editions_reuse_analyze_and_resume_independently(
    tmp_path: Path,
) -> None:
    workspace, settings, analyze_task_id = _workspace_with_material_curriculum(tmp_path)
    base_options = workspace.canonical_record("curriculum-options")
    base_selection = workspace.canonical_record("selected-curriculum")
    assert base_options is not None and base_selection is not None

    english = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        output_language_override="English",
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "conviction-en",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
        edition_name="english-edition",
        curriculum_path_id="thematic",
        skill_name="conviction-en",
    )
    chinese = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        output_language_override="Chinese",
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "conviction-zh",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
        edition_name="chinese-application",
        curriculum_path_id="application-first",
        skill_name="conviction-zh",
    )
    english_view = _edition_view(workspace, "english-edition")
    chinese_view = _edition_view(workspace, "chinese-application")

    assert english.edition_id != chinese.edition_id
    assert all(
        not english_view.list_work_items(run.id, role=WorkRole.ANALYZE)
        for run in [english_view.create_analysis_run(settings=settings)]
    )
    assert all(
        not chinese_view.list_work_items(run.id, role=WorkRole.ANALYZE)
        for run in [chinese_view.create_analysis_run(settings=settings)]
    )
    assert workspace.canonical_record("curriculum-options") == base_options
    assert workspace.canonical_record("selected-curriculum") == base_selection
    english_selection_record = english_view.canonical_record("selected-curriculum")
    chinese_selection_record = chinese_view.canonical_record("selected-curriculum")
    assert english_selection_record is not None and chinese_selection_record is not None
    english_selected = json.loads(
        (workspace.root / english_selection_record.path).read_text(encoding="utf-8")
    )
    chinese_selected = json.loads(
        (workspace.root / chinese_selection_record.path).read_text(encoding="utf-8")
    )
    assert english_selected["selected_path_id"] == "thematic"
    assert chinese_selected["selected_path_id"] == "application-first"
    assert english_selected["source"] == chinese_selected["source"] == "edition"

    _submit_edition_author(
        workspace,
        english_view,
        english,
        selected_path_id="thematic",
        artifact_language="English",
        skill_name="conviction-en",
    )
    _submit_edition_author(
        workspace,
        chinese_view,
        chinese,
        selected_path_id="application-first",
        artifact_language="Chinese",
        skill_name="conviction-zh",
    )

    english_complete = _finish_passing_edition(
        workspace,
        english_view,
        settings,
        "english-edition",
    )
    assert english_complete.status == "complete"
    english_completion = dict(english_complete.completion or {})

    chinese_trials = _resume_edition(workspace, settings, "chinese-application")
    _submit_trial_envelope(chinese_view, chinese_trials, run_prefix="chinese-first")
    chinese_review = _resume_edition(workspace, settings, "chinese-application")
    [review_action] = chinese_review.actions
    review_task = chinese_view.get_work_item(review_action.task_id)
    review_lease = json.loads((review_action.task_path / "lease.json").read_text(encoding="utf-8"))
    failed_review_path = review_action.task_path / "output" / "result.json"
    atomic_write_json(
        failed_review_path,
        _review_result(
            chinese_view,
            review_task,
            str(review_lease["lease_token"]),
            verdict="fail",
        ),
    )
    submit_workspace_result(workspace, review_task.id, failed_review_path)

    repair = _resume_edition(workspace, settings, "chinese-application")
    repair_task = chinese_view.get_work_item(repair.actions[0].task_id)
    assert repair_task.scope["kind"] == "course-authoring-repair"
    assert _resume_edition(workspace, settings, "english-edition").status == "complete"
    _submit_edition_author(
        workspace,
        chinese_view,
        repair,
        selected_path_id="application-first",
        artifact_language="Chinese",
        skill_name="conviction-zh",
    )
    chinese_complete = _finish_passing_edition(
        workspace,
        chinese_view,
        settings,
        "chinese-application",
    )
    assert chinese_complete.status == "complete"

    english_manifest = json.loads(
        (
            Path(str(english_complete.completion["generated_path"])) / "build-manifest.json"
        ).read_text(encoding="utf-8")
    )
    chinese_manifest = json.loads(
        (
            Path(str(chinese_complete.completion["generated_path"])) / "build-manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert english_manifest["artifact_language"] == "English"
    assert chinese_manifest["artifact_language"] == "Chinese"
    assert english_manifest["parent_build_id"] is None
    assert chinese_manifest["parent_build_id"] is None
    assert (
        english_manifest["edition_lineage"]["canonical_analyze_digests"]
        == chinese_manifest["edition_lineage"]["canonical_analyze_digests"]
    )
    assert (
        english_manifest["edition_lineage"]["analyze_task_id"]
        == chinese_manifest["edition_lineage"]["analyze_task_id"]
        == analyze_task_id
    )
    assert english_manifest["semantic_map_digest"] == chinese_manifest["semantic_map_digest"]
    assert set(english_manifest["managed_files"]) == set(chinese_manifest["managed_files"])
    assert {
        value.get("artifact_id")
        for value in english_manifest["managed_files"].values()
        if "artifact_id" in value
    } == {
        value.get("artifact_id")
        for value in chinese_manifest["managed_files"].values()
        if "artifact_id" in value
    }
    english_provenance = json.loads(
        (Path(str(english_complete.completion["generated_path"])) / "provenance.json").read_text(
            encoding="utf-8"
        )
    )
    chinese_provenance = json.loads(
        (Path(str(chinese_complete.completion["generated_path"])) / "provenance.json").read_text(
            encoding="utf-8"
        )
    )
    assert [claim["id"] for claim in english_provenance["claims"]] == [
        claim["id"] for claim in chinese_provenance["claims"]
    ]
    assert [claim["evidence"] for claim in english_provenance["claims"]] == [
        claim["evidence"] for claim in chinese_provenance["claims"]
    ]
    assert not any(
        "transcript" in path or path.startswith("sources/")
        for path in english_manifest["managed_files"]
    )

    chinese_generated = Path(str(chinese_complete.completion["generated_path"]))
    (chinese_generated / "human-note.md").write_text("keep", encoding="utf-8")
    conflict = _resume_edition(workspace, settings, "chinese-application")
    assert conflict.status == "actions-required"
    assert conflict.actions[0].kind == "ask-user"
    assert (chinese_generated / "human-note.md").read_text(encoding="utf-8") == "keep"
    english_resumed = _resume_edition(workspace, settings, "english-edition")
    assert english_resumed.status == "complete"
    assert english_resumed.completion["build_id"] == english_completion["build_id"]
    assert english_resumed.completion["generated_path"] == english_completion["generated_path"]


def test_edition_can_plan_from_analyze_only_and_rejects_stale_lineage(
    tmp_path: Path,
) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)
    planned = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        output_language_override="French",
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "planned-fr",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
        edition_name="planned-french",
        plan_curriculum=True,
        skill_name="planned-fr",
    )
    view = _edition_view(workspace, "planned-french")
    [action] = planned.actions
    task = view.get_work_item(action.task_id)
    assert task.scope["kind"] == "curriculum-planning"
    run = view.create_analysis_run(settings=settings)
    assert view.list_work_items(run.id, role=WorkRole.ANALYZE) == []
    assert task.dependencies == [analyze_task.id]

    semantic_record = workspace.canonical_record("semantic-map")
    assert semantic_record is not None
    semantic = json.loads((workspace.root / semantic_record.path).read_text(encoding="utf-8"))
    semantic[0]["summary"] = "A refreshed Analyze interpretation."
    replacement = workspace.analysis_dir / "refreshed-semantic-map.json"
    atomic_write_json(replacement, semantic)
    workspace.publish_canonical_record(
        kind="semantic-map",
        record_id="default",
        source_path=replacement,
        producer_task_id=analyze_task.id,
        snapshot_digest=analyze_task.snapshot_digest,
    )

    with pytest.raises(ProcessingError, match="pinned to different Analyze state"):
        _resume_edition(workspace, settings, "planned-french")
