from __future__ import annotations

import json
from pathlib import Path

from test_author import _analyzed_workspace, _author_result, _curriculum_result
from test_review import _review_result

from video_to_skill.config import Settings
from video_to_skill.coordinator import advance_run, submit_workspace_result
from video_to_skill.decompose import (
    SkillCandidate,
    SkillDecompositionResult,
    SkillPackPlan,
)
from video_to_skill.installation import SkillHost
from video_to_skill.models import ObservationProducer
from video_to_skill.orchestration import DecisionResult
from video_to_skill.utils import atomic_write_json
from video_to_skill.workspace import Workspace


def _resume(workspace: Workspace, settings: Settings):
    return advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=None,
    )


def test_multi_skill_parallel_pipeline_builds_and_installs_pack(tmp_path: Path) -> None:
    workspace, _analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)
    pack_output = tmp_path / "generated"
    skill_root = tmp_path / "skills"

    # 1. Decompose step is dispatched automatically after Analyze.
    decompose_env = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=SkillHost.CODEX,
        output=pack_output,
        skill_root=skill_root,
        run_official_validation=False,
    )
    assert decompose_env.status == "actions-required"
    [decompose_action] = decompose_env.actions
    decompose_task = workspace.get_work_item(decompose_action.task_id)
    assert decompose_task.scope["kind"] == "skill-decomposition"
    decompose_lease = json.loads(
        (decompose_action.task_path / "lease.json").read_text(encoding="utf-8")
    )
    decompose_result_path = decompose_action.task_path / "output" / "result.json"
    atomic_write_json(
        decompose_result_path,
        SkillDecompositionResult(
            task_id=decompose_task.id,
            lease_token=str(decompose_lease["lease_token"]),
            snapshot_digest=decompose_task.snapshot_digest,
            producer=ObservationProducer(name="decompose-worker", run_id="decompose-run"),
            plan=SkillPackPlan(
                decomposition_mode="multi",
                rationale="Two distinct workflows that users invoke independently.",
                skills=[
                    SkillCandidate(
                        id="conviction-calibration",
                        name="conviction-calibration",
                        title="Conviction Calibration",
                        description="Calibrate belief strength against incoming evidence.",
                        rationale="Independent decision-hygiene skill.",
                        semantic_unit_ids=["unit-conviction"],
                    ),
                    SkillCandidate(
                        id="decision-postmortem",
                        name="decision-postmortem",
                        title="Decision Postmortem",
                        description="Audit past decisions separating process from outcome.",
                        rationale="Independent retrospective skill.",
                        semantic_unit_ids=["unit-conviction"],
                        prerequisites=["conviction-calibration"],
                        estimated_scope="focused",
                    ),
                ],
            ),
        ),
    )
    submit_workspace_result(workspace, decompose_task.id, decompose_result_path)

    # 2. Multi-skill decomposition asks user which Skills to build.
    selection_env = _resume(workspace, settings)
    assert selection_env.status == "actions-required"
    [selection_action] = selection_env.actions
    assert selection_action.kind == "ask-user"
    assert selection_action.options[0]["id"] == "all"
    selection_task = workspace.get_work_item(selection_action.task_id)
    selection_lease = json.loads(
        (selection_action.task_path / "lease.json").read_text(encoding="utf-8")
    )
    selection_result_path = selection_action.task_path / "output" / "result.json"
    atomic_write_json(
        selection_result_path,
        DecisionResult(
            task_id=selection_task.id,
            lease_token=str(selection_lease["lease_token"]),
            snapshot_digest=selection_task.snapshot_digest,
            producer=ObservationProducer(name="user"),
            selected_option_id="all",
        ),
    )
    submit_workspace_result(workspace, selection_task.id, selection_result_path)

    # 3. Parallel curriculum planning: BOTH skills dispatched in a single envelope.
    curricula_env = _resume(workspace, settings)
    assert curricula_env.status == "actions-required"
    assert len(curricula_env.actions) == 2
    assert len({a.parallel_group for a in curricula_env.actions}) == 1
    for action in curricula_env.actions:
        scoped_id = workspace.task_edition_id(action.task_id)
        assert scoped_id is not None and scoped_id.startswith("skill-")
        skill_view = workspace.for_edition(scoped_id)
        task = skill_view.get_work_item(action.task_id)
        assert task.scope["kind"] == "curriculum-planning"
        lease = json.loads((action.task_path / "lease.json").read_text(encoding="utf-8"))
        result_path = action.task_path / "output" / "result.json"
        atomic_write_json(
            result_path,
            _curriculum_result(task, str(lease["lease_token"]), decision_required=False),
        )
        # Submit via base workspace — it routes automatically to the skill scope!
        submit_workspace_result(workspace, task.id, result_path)

    # 4. Parallel authoring: BOTH skills dispatched in a single envelope, no extra prompts.
    author_env = _resume(workspace, settings)
    assert author_env.status == "actions-required"
    assert len(author_env.actions) == 2
    skill_names_by_id = {
        "conviction-calibration": "conviction-calibration",
        "decision-postmortem": "decision-postmortem",
    }
    for action in author_env.actions:
        scoped_id = workspace.task_edition_id(action.task_id)
        assert scoped_id is not None
        skill_view = workspace.for_edition(scoped_id)
        task = skill_view.get_work_item(action.task_id)
        assert task.scope["kind"] == "course-authoring"
        skill_id = str(task.scope["skill_id"])
        skill_name = skill_names_by_id[skill_id]
        lease = json.loads((action.task_path / "lease.json").read_text(encoding="utf-8"))
        draft = action.task_path / "output" / "course.md"
        draft.write_text(
            f"# {skill_name}\n\nFocused instructions for {skill_name}.\n",
            encoding="utf-8",
        )
        author_res = _author_result(task, str(lease["lease_token"]), draft).model_copy(
            update={"name": skill_name, "title": skill_name.replace("-", " ").title()}
        )
        result_path = action.task_path / "output" / "result.json"
        atomic_write_json(result_path, author_res)
        submit_workspace_result(workspace, task.id, result_path)

    # 5. Parallel lightweight review: 1 Review per skill, both in ONE envelope!
    review_env = _resume(workspace, settings)
    assert review_env.status == "actions-required"
    assert len(review_env.actions) == 2
    for action in review_env.actions:
        assert action.role == "review"
        scoped_id = workspace.task_edition_id(action.task_id)
        assert scoped_id is not None
        skill_view = workspace.for_edition(scoped_id)
        task = skill_view.get_work_item(action.task_id)
        assert task.scope["kind"] == "independent-review"
        lease = json.loads((action.task_path / "lease.json").read_text(encoding="utf-8"))
        result_path = action.task_path / "output" / "result.json"
        atomic_write_json(
            result_path,
            _review_result(skill_view, task, str(lease["lease_token"])),
        )
        submit_workspace_result(workspace, task.id, result_path)

    # 6. Batch compile, validate, install, and emit pack-manifest.json!
    complete = _resume(workspace, settings)
    assert complete.status == "complete"
    assert complete.completion is not None
    assert complete.completion["mode"] == "multi"
    assert complete.completion["skill_count"] == 2
    manifest_path = Path(str(complete.completion["pack_manifest_path"]))
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["skill_count"] == 2
    installed_names = [entry["name"] for entry in manifest["skills"]]
    assert installed_names == ["conviction-calibration", "decision-postmortem"]
    for entry in manifest["skills"]:
        assert (Path(entry["generated_path"]) / "SKILL.md").is_file()
        assert (Path(entry["installed_path"]) / "SKILL.md").is_file()
