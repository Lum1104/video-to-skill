---
name: video-to-skill
description: "Turn a video, tutorial, playlist, or course into one or more installed, evidence-grounded Skills that can teach, give practice and feedback, apply demonstrated methods, and answer reference questions. Automatically decomposes long or multi-topic videos into focused Skills built in parallel. Use when the user provides video sources and wants reusable learning or operational capability."
---

<!-- argument-hint: <video-url-or-local-path>... [skill-name] -->

# Video-to-Skill

Own the complete workflow from source discovery through installation of one or more focused, reusable Skills—not a transcript summary.

## Core Rules

Normal invocation:

```text
/video-to-skill <URL>     # Claude Code
$video-to-skill <URL>     # Codex
```

- **Default scope**: Process every accessible playlist or course item by default, preserve playlist order, resume cached work after interruption, and keep the private evidence workspace separate from the portable Skill(s). Continue through isolated source failures when useful material remains and report any coverage gaps.
- **Ask only at real boundaries**: Ask only when credentials are needed, local/private media would leave the machine, a billed dependency is required, scope is unexpectedly large, a long/multi-topic video decomposes into multiple Skills for selection (`skill-pack-selection`), the user must pick between materially different curricula, or an installation name conflicts with different content. Never ask the user to run internal commands, tune frame settings, copy task payloads, or invoke a second tutor Skill. Never request an account password.
- **Output language**: Infer one canonical artifact language from the user's request/conversation language (or honor an explicit language). Keep this separate from source caption/ASR `--language`.
- **Analysis depth**: Use `--analysis-depth auto` by default (selects `standard` or `deep` from duration, item count, chapters, captions, and visual signals). Use `archival` only when the user explicitly accepts its higher private storage boundary.
- **No silent overwrites**: Never overwrite an existing different Skill or discard human edits. Update/fold-in into an existing Skill requires explicit `--force` or a new Skill name.
- **Division of labor**: The engine never calls an LLM—deterministic Python owns acquisition, packets, task state, validation, compilation, rendering, and installation. The host main agent dispatches native workers for `Analyze → Decompose → Author → Review`.

## Start the Engine

Resolve the directory containing this `SKILL.md` to an absolute `SKILL_DIR` (do not infer from the current working directory):

```bash
V2S_ENGINE="$SKILL_DIR/scripts/video-to-skill"   # Windows: scripts\video-to-skill.cmd
"$V2S_ENGINE" --help
```

The launcher manages its private Python runtime automatically. Do not activate a virtualenv, run pip/uv, or use `eval`. If bootstrap reports a missing Python, network failure, or broken runtime, report that exact condition and stop.

Choose a durable workspace outside the generated output and installed Skill. Use the current host automatically when known.

## Run the Workflow

Start a new conversion:

```bash
"$V2S_ENGINE" run SOURCES... --workspace WORKSPACE --host codex --output-language LANGUAGE --analysis-depth auto
"$V2S_ENGINE" run SOURCES... --workspace WORKSPACE --host claude --output-language LANGUAGE --analysis-depth auto
```

Add `--project` only when the user asks for project-local installation, and `--output` only when they specify a portable output path. Resume without retransmitting sources or options:

```bash
"$V2S_ENGINE" run --workspace WORKSPACE
```

`run` performs all available deterministic transitions and outputs one JSON envelope:

### When status is `actions-required`

Dispatch independent actions in parallel when the host supports workers. For each `dispatch-agent` action, use its exact `persona_hint`, absolute `task_path`, `role`, and task ID—never copy large packets or drafts through the main conversation:

```text
Use the assigned persona.
Open TASK_PATH/task.json, packet.json, result-schema.json, and lease.json.
Perform the bounded task from workspace evidence.
Write the strict result to TASK_PATH/output/result.json.
Run ENGINE submit WORKSPACE TASK_ID TASK_PATH/output/result.json yourself.
Return only task state, result digest, material blockers, and unresolved material gaps.
```

Workers may run bounded `context`, `contact-sheet`, `frames`, `query`, and `gaps` commands permitted by their packet, and must never write SQLite directly.

For an `ask-user` action, ask the supplied prompt with its options, write the chosen decision to the task's result JSON, and submit it.

After dispatched workers finish, run `"$V2S_ENGINE" run --workspace WORKSPACE` again and repeat until `complete`.

### When status is `complete`

Report:
- Installed Skill name(s), path(s), and invocation (`/<name>` or `$<name>`); for multi-skill packs, also note `pack-manifest.json`
- Workspace path (retained by default; `clean` is always user-initiated)
- Processed vs. failed/partial/inaccessible sources, source vs. semantic coverage, and any capability limits
- Resolved artifact language, requested/effective analysis depth, instructional-affordance coverage, critic repair count, build ID, and validation status
- One or two concrete next actions using the installed Skill name (e.g. `/<course-skill> start`).

Only the engine's `complete` envelope proves that the Skill(s) compiled, validated, and installed.

## Pipeline Overview (`Analyze → Decompose → Author → Review`)

- **Analyze**: Extract grounded semantic units, relations, conflicts, visual teaching candidates (≤24), and capability ceilings from workspace evidence.
- **Decompose**: Decide whether the analyzed material belongs in one focused Skill (auto-continues with zero extra prompt) or decomposes into multiple independent Skills (pauses once at `skill-pack-selection` so the user can build all or pick a subset). Selected Skills build in parallel inside isolated `skill-*` scopes and finish with a shared `pack-manifest.json`.
- **Author**: Plan a curriculum (pausing for `ask-user` only if alternates materially differ in single-skill mode), then author grounded artifacts and complete the instructional-affordance ledger. Treat Learn, Practice, Apply, and Reference as evidence-bounded capability levels, not artifact quotas.
- **Review**: Dispatch one independent critic per Skill against the immutable rendered preview and canonical evidence (up to 2 repair cycles).

## Detailed References (Load On Demand)

Load these only when needed for the current step:
- [Worker protocol, evidence rules & generated Skill contract](references/worker-protocol.md) — detailed Analyze/Author/Review rules and quality floor.
- [Named editions & evidence bundles](references/editions-and-bundles.md) — publishing another language/curriculum edition from the same Analyze map, or exporting/verifying `.v2sbundle` archives.
- [Authentication, secrets & failure recovery](references/auth-and-recovery.md) — browser cookies, secret handling, `tool-runs`, and recovery from failures or rejections.
