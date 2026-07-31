---
name: video-to-skill
description: "Turn a video, tutorial, playlist, or course into an installed, evidence-grounded course Skill that can teach, give practice and feedback, apply demonstrated methods, and answer reference questions. Use when the user provides video sources and wants reusable learning or operational capability."
---

<!-- argument-hint: <video-url-or-local-path>... [skill-name] -->

# Video-to-Skill

Convert demonstrated capability, not a transcript summary. Own the complete workflow from accessible source discovery through installation of one course-specific Skill.

## User contract

A normal invocation is:

```text
/video-to-skill <URL>     # Claude Code
$video-to-skill <URL>     # Codex
```

Treat supplied URLs or local paths as authorization for reversible local processing. Process every accessible playlist or course item by default, resume cached work after interruption, and keep the private evidence workspace separate from the portable Skill.

Ask only when a material boundary cannot be inferred safely: credentials are needed, local or private media would leave the machine, a billed dependency is required, scope is unexpectedly large, the user must choose between materially different curricula, or an installation name conflicts with different content.

Never ask the user to run internal commands, supervise extraction, select frame parameters, copy task payloads, or invoke a second tutor Skill. Never request an account password.

The engine is model-agnostic and never calls an LLM. The host main agent dispatches native workers for semantic, multimodal, pedagogical, and critical judgment; deterministic code owns acquisition, bounded packets, task state, validation, compilation, rendering, and installation.

## Start the engine

Resolve the directory containing this `SKILL.md` to an absolute `SKILL_DIR`. Do not infer it from the conversation working directory.

```text
video-to-skill/
├── SKILL.md
├── scripts/
│   ├── video-to-skill
│   └── video-to-skill.cmd
├── pyproject.toml + src/ + scripts/video_to_skill.py
└── runtime.json + scripts/engine.py
```

On POSIX, bind and verify the absolute launcher:

```bash
V2S_ENGINE="$SKILL_DIR/scripts/video-to-skill"
"$V2S_ENGINE" --help
```

On Windows, use `scripts\video-to-skill.cmd`. The launcher owns first-use bootstrap, its private Python runtime, bounded repair, and optional capability installation. Do not activate an environment, invoke a package manager, or use `eval`.

Choose a durable workspace outside the generated output and installed Skill. Use the current host without asking when it is known.

## Run the durable workflow

Start a new conversion:

```bash
"$V2S_ENGINE" run SOURCES... --workspace WORKSPACE --host codex
"$V2S_ENGINE" run SOURCES... --workspace WORKSPACE --host claude
```

Add `--project` only when the user requested project-local installation. Add `--output` only when the user supplied a portable output path. Resume without retransmitting sources or configuration:

```bash
"$V2S_ENGINE" run --workspace WORKSPACE
```

`run` performs every available deterministic transition and emits one JSON envelope.

### `actions-required`

Dispatch every independent action in the returned parallel group when the host supports workers.

For a `dispatch-agent` action, use its exact `persona_hint`, absolute `task_path`, `role`, and task ID. Do not read or copy its packet through the main-agent conversation.

Give the worker only:

```text
Use the assigned expert persona.
Open TASK_PATH/task.json, packet.json, result-schema.json, and lease.json.
Perform the bounded task from workspace evidence.
Write the strict result to TASK_PATH/output/result.json.
Run ENGINE submit WORKSPACE TASK_ID TASK_PATH/output/result.json yourself.
Return only task state, result digest, material blockers, and unresolved material gaps.
```

The worker may use bounded `context`, `contact-sheet`, `frames`, `query`, and `gaps` commands when the task packet permits them. It must never write SQLite directly, inspect unrelated workspace material, return private chain-of-thought, or send large semantic results and drafts through the main agent.

For an `ask-user` action, ask the supplied prompt with its supplied options. Write the selected option to the task's strict decision result, submit it, and do not reopen settled implementation details.

After all dispatched workers finish, call `run --workspace WORKSPACE` again. Repeat until `complete` or an ordinary command failure.

### `complete`

Report the installed Skill name and path, invocation, workspace path and retention, processed and failed source counts, source and semantic coverage, instructional-affordance coverage, critic repair count, build ID, and validation results.

Do not claim completion from a worker message. Only the engine's `complete` envelope proves that canonical state compiled, validated, and installed.

## Worker roles

The logical workflow is `Analyze → Author → Review`. These are reasoning boundaries, not public commands or user-facing modes.

### Analyze

Use a senior evidence and semantic-analysis expert. Combine high-recall extraction, terminology normalization, relation linking, materiality review, disposition accounting, conflict capture, and capability-ceiling analysis in one bounded role.

Preserve questions, claims, reasons, examples, analogies, definitions, distinctions, qualifications, counterpoints, predictions, recommendations, warnings, value judgments, and open questions. Preserve relations that answer, support, explain, exemplify, qualify, contrast, depend on, update, contradict, raise, or leave another unit unresolved.

Use speech-first packets for interviews and talking heads. Inspect slides, code, UI, diagrams, or physical state only when visual evidence is material. For long courses, accept section-group tasks and then perform a dependent integration Analyze task without deleting source-specific semantic units.

Every semantic unit needs a stable ID, source and time range, kind, compact summary, materiality, disposition, modality, evidence IDs, observed-versus-inferred status, confidence, and uncertainty. Merged, context-only, and omitted material needs an explicit reason.

### Author

Use a principal learning-science and Agent Skill author. Read canonical semantic records from the task packet, design a thematic default and justified alternate paths, and write Markdown drafts directly inside the task output directory.

Treat Learn, Practice, Apply, and Reference as evidence-bounded capability levels, not artifact quotas. Never exceed the Analyze capability ceiling.

Complete the instructional-affordance ledger independently from semantic coverage. Account for learning objectives, misconceptions, retrieval and transfer prompts, focused exercises, success criteria, scored rubrics, progressive hints, retry, capstone synthesis, operational playbooks, expected states, validation, recovery, quick reference, and decision rules.

Mark each affordance `provided`, `unsupported`, or `not-applicable` with a rationale. Strong capability claims require the complete corresponding surface; weaker or unsupported claims remain honest. Multiple affordances may live in one independently useful artifact.

Give every artifact a stable ID, user job, supported behaviors, normal or after-attempt disclosure, independent loading reason, semantic-unit links, affordance links, destination path, draft path, and digest. Keep solutions and answer-bearing rubrics separate and after-attempt.

### Review

Use a fresh senior Agent Skill critic who is independent of the Author producer. Review the actual canonical drafts and records, not an author-supplied summary.

Audit source-meaning retention and instructional-affordance retention separately, then grounding, uncertainty, disclosure, empty invocation, runtime behavior, safety, scope, source failures, and shareability.

A failed Review completes its execution task but creates a new immutable Author revision and a fresh independent Review. Allow at most three repair cycles. Never weaken a capability claim merely to hide an affordance the evidence supports and the product needs.

## Evidence rules

- Speech establishes stated intent or explanation.
- A visible-state claim requires visual evidence.
- An action or transition normally requires ordered before and after evidence.
- A successful procedure requires the action and an observable result.
- Exact code, commands, labels, or values remain uncertain when illegible.
- Two probes that add no evidence are a stopping signal, not permission to guess.
- Low-confidence exact details never become authoritative instructions.
- Source-acquisition coverage and semantic coverage remain separate.
- Every material semantic unit receives a disposition.
- Every included core or supporting unit appears in a grounded artifact.

Use native host multimodal inspection before any hosted vision provider. Keep hosted vision disabled unless native viewing is unavailable or the user authorized the privacy and cost boundary.

## Authentication and secrets

When a source needs login, negotiate authentication once for the run: a named browser/profile, a local Netscape `cookies.txt`, or public items only. Set `VIDEO_TO_SKILL_COOKIES_FROM_BROWSER` only for the engine invocation.

Treat cookies, headers, tokens, expiring URLs, and browser snapshots as runtime secrets. Never quote them back, place them in task packets or logs, persist them in the workspace, or include them in the generated Skill.

## Generated Skill contract

The portable package and raw workspace must be separate trees. Every generated course Skill contains five fixed root records and at least one authored Markdown artifact. A representative evidence-justified package can contain:

```text
<course-skill>/
├── SKILL.md
├── source-map.md
├── sources.md
├── provenance.json
├── build-manifest.json
├── chapters/
│   └── <topic>.md
├── exercises/
│   └── <exercise>.md
├── solutions/
│   └── <exercise>.md
├── playbooks/
│   └── <workflow>.md
├── reference/
│   └── <decision-aid>.md
├── learning-path.md
├── glossary.md
├── patterns.md
├── cheatsheet.md
└── assets/
    └── <indispensable-image>.png
```

The five fixed root records are unconditional; the remaining entries illustrate supported artifact shapes rather than a directory quota. Use `chapters/` for independently loadable teaching units, `exercises/` for practice, `solutions/` for after-attempt answers and answer-bearing rubrics, `playbooks/` for operational application, `reference/` and the allowed root reference files for fast lookup, and `assets/` only for indispensable images. A capable source should normally produce several substantial artifacts across the behaviors it genuinely supports, while a compact source may justify fewer.

Do not manufacture symmetrical directories, split files that always load together, or omit useful material merely to keep the package small. Every included artifact needs evidence links, covered affordances, and an independent loading reason. Never include raw video, audio, complete subtitles, transcripts, databases, cookies, caches, or decorative frame collections.

Mark generated Skills with:

```html
<!-- video-to-skill:course-skill:v2 -->
```

Keep the resident `SKILL.md` concise and operational. On empty invocation it offers `start`, loads no supporting file, inspects no project, runs no command, creates no file, and waits. During use it adapts naturally across learning, practice, application, and reference without presenting a mode menu.

Use source-grounded material first, label generator-created exercises or adaptations as inference, distinguish outside or current knowledge, answer in the user's language, and preserve honest uncertainty.

The compiler derives the strict blueprint from canonical workspace state, reads artifact bodies by verified relative path and digest, renders outside the workspace, validates structure and behavior, and installs without overwriting different same-name content.

Keep the workspace by default. `clean` remains an explicit user action.
