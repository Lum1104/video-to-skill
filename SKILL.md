---
name: video-to-skill
description: "Turn a video, tutorial, playlist, or course into an installed, evidence-grounded course Skill that can teach, give practice and feedback, apply the demonstrated methods, and answer reference questions. Use when the user provides video sources and wants reusable learning or operational capability."
---
<!-- argument-hint: <video-url-or-local-path>... [skill-name] -->
# Video-to-Skill

Convert demonstrated capability, not a transcript summary. One invocation owns the complete workflow from accessible source discovery through installation of a course-specific Skill.

## User contract

A normal invocation is:

```text
/video-to-skill <URL>     # Claude Code
$video-to-skill <URL>     # Codex
```

Treat the supplied URL or local path as authorization to perform all reversible local processing needed for the result. Do not ask the user to run internal CLI commands, choose keyframe parameters, supervise intermediate stages, or invoke a second tutor Skill.

For a playlist, collection, or course, enumerate and process every accessible item by default. Download the captions, metadata, audio, and analysis-quality video needed for transcription and multimodal investigation. Prefer a bounded working copy over archival maximum quality. Resume interrupted work and reuse cached evidence.

Ask only when a material boundary cannot be inferred safely: private or paid content needs new credentials, local/private media would be uploaded to a hosted service, a new billed dependency is required, the resolved scope is unexpectedly large, or a conflicting same-name Skill already contains different material. Continue through partial source failures and report their impact.

The user interacts with two layers:

1. `video-to-skill` is the generator and evidence compiler.
2. The generated course-specific Skill is the teacher and practitioner. It must support Learn, Practice, Apply, and Reference itself.

Do not create a second generic video tutor Skill for the MVP.

## Modes

### Full conversion

Default when the user supplies new sources. Inspect, acquire, transcribe, investigate, close material evidence gaps, synthesize, render, critique, validate, and install.

### Analyze only

Use only when the user explicitly asks to inspect or analyze without creating a Skill. Stop after the capability map, coverage report, conflicts, and material gaps.

### Generate from workspace

When given an existing evidence workspace without new media, verify its inventory and gaps, resolve only material gaps, then synthesize, validate, and install.

### Update or fold-in

When given an existing generated Skill plus new evidence, extract the new inputs into a separate linked workspace, preserve supported and user-authored material, merge at the capability and provenance level, and validate the staged result. The no-clobber installer can install a fresh name or confirm identical content; if different content already exists under that name, retain the staged update and report the conflict instead of replacing the installed Skill. Never delete the original as an update strategy.

## 1. Start the bundled internal engine

Resolve the directory containing this `SKILL.md` to an absolute path named `SKILL_DIR`. Do not infer it from the conversation's current working directory. Standard Agent Skills installers copy the source-form Skill, while the Python package installer creates a compact runtime-bound bundle. Both forms provide the same launcher:

```text
video-to-skill/
├── SKILL.md
├── scripts/
│   ├── video-to-skill
│   └── video-to-skill.cmd
├── pyproject.toml + src/ + scripts/video_to_skill.py   # source form
└── runtime.json + scripts/engine.py                    # compact bundle form
```

On POSIX hosts, bind the absolute launcher and verify it:

```bash
V2S_ENGINE="$SKILL_DIR/scripts/video-to-skill"
"$V2S_ENGINE" --help
```

On Windows hosts, use `scripts\video-to-skill.cmd` relative to `SKILL_DIR`. In source form, the first launcher call automatically creates a fingerprinted private runtime in the user's platform data directory and installs the local engine into it; later calls reuse it. The fingerprint covers the Python minor version, project metadata, bootstrap engine, launchers, and installable source files. Before reuse, the launcher runs a bounded core import probe. If that probe detects a damaged runtime, the launcher removes and rebuilds that one runtime once; it never enters an automatic repair loop. In compact bundle form, `runtime.json` records the already-bound Python environment. The launcher owns both paths.

Every Bash snippet below uses `"$V2S_ENGINE"` as shorthand for that resolved absolute launcher. Substitute the actual absolute path in each process call rather than relying on a shell variable surviving between calls. Pass the launcher and every source or option as separate arguments; never concatenate source text into executable shell syntax or use `eval`.

Do not activate an environment, run a package manager yourself, or ask the user to run an internal command. The source launcher owns first-use bootstrap and repair; invoke it normally and let it report a missing supported Python or network failure. If the launcher itself is missing, report that the installed Skill is incomplete. If a compact bundle's recorded Python environment was removed, report that the bundle must be repaired or reinstalled. Do not pretend extraction occurred.

## 2. Resolve the complete source set

Run:

```bash
"$V2S_ENGINE" doctor
"$V2S_ENGINE" inspect SOURCES...
```

Record the resolved item count, playlist order, duration, caption languages, unavailable items, expected ASR route, and likely visual route. Inspection is not a confirmation gate when the scope is ordinary and processing stays local.

The source-form launcher keeps heavy local providers out of first-use setup. After inspection determines the evidence route, invoke only the capabilities that route needs:

```bash
"$V2S_ENGINE" ensure-capability asr
"$V2S_ENGINE" ensure-capability ocr
```

Run `ensure-capability asr` only when one or more accessible items lack adequate captions and local transcription is required. Run `ensure-capability ocr` only when the chosen coding, UI, slide, diagram, or physical-procedure route needs machine-readable on-screen text; native multimodal viewing does not itself require OCR. Run `ensure-capability diarization` only when speaker separation is materially necessary. Each command is an internal, idempotent launcher operation: it installs the selected extra from this local source-form Skill into the same private runtime, verifies it with a bounded import probe, and suppresses package-manager details. It may download the selected provider's third-party dependencies, so do not install all capabilities speculatively. Never ask the user to run these commands or run `pip`.

Infer the dominant source type per semantic section:

| Type | Evidence priority | Investigation |
|---|---|---|
| Coding | Spoken intent, legible code state, edit and run results | Before/edit/after frames around changes, commands, errors, and fixes |
| Software/UI | Labels, values, inputs, navigation, saved result | State-transition windows around consequential interactions |
| Slides/lecture | Speech, slide text, diagrams, examples | Slide changes, OCR, and dense frames only for evolving diagrams |
| Physical procedure | Object state, hand/tool action, before/after condition | Ordered state changes and safety-relevant omissions |
| Mixed | Section-specific combination | Route each section independently |

Use all four generated Skill modes regardless of the dominant type. The user's wording may change which artifacts receive the most depth, but it must not remove Learn, Practice, Apply, or Reference behavior.

## 3. Extract or resume evidence

Choose a workspace outside the shareable Skill package and run:

```bash
"$V2S_ENGINE" extract SOURCES... --workspace WORKSPACE
```

Reuse the same workspace after interruption. Do not recreate it because one item failed. The workspace may contain media, subtitles, frames, databases, and agent observations; none of those raw artifacts belong in the shareable package.

A workspace's input list is immutable. Resume it only with the same inputs. When an update introduces new or changed sources, create a separate linked workspace and merge the resulting capability records and provenance into the staged Skill; do not append the new inputs to the old workspace.

For each resolved course item, record complete, partial, failed, or skipped status. Do not silently omit inaccessible videos. A partial course can still produce a Skill when its coverage limits are explicit.

For an existing workspace, begin with:

```bash
"$V2S_ENGINE" query WORKSPACE --inventory
"$V2S_ENGINE" gaps WORKSPACE --format json
```

Treat the workspace as a queryable evidence corpus. Never load a long transcript wholesale.

## 4. Orchestrate expert investigation

When the host supports subagents, delegate bounded work with explicit expert personas and the strongest available multimodal/reasoning model:

- A senior multimodal evidence investigator for coding, UI, slides, or physical procedures, matched to each source type.
- A principal learning-science and curriculum architect to turn the evidence graph into adaptive lessons and mastery checks.
- A senior Agent Skill critic to audit grounding, progressive disclosure, safety, and host usability independently of the authoring pass.

Parallelize independent sources or sections. Give each investigator only its inventory row, synchronized context packet, contact sheet, open questions, and evidence budget. Require structured claims with evidence IDs, timestamps, confidence, and uncertainty; never request private chain-of-thought. The main agent owns cross-source terminology, conflicts, provenance, and the final installation.

For one short source, one investigator plus an independent critic is enough. Do not create agents merely to restate deterministic CLI output.

## 5. Run the multimodal evidence loop

Set a bounded investigation budget. By default, use at most two escalation passes per unresolved gap, dense windows no longer than 30 seconds at no more than 4 fps, and at most 12 dense windows per source. Expand only for a central or safety-relevant capability.

For each source or semantic section:

1. Retrieve the inventory and bounded context:

```bash
"$V2S_ENGINE" query WORKSPACE --source ID --section N
"$V2S_ENGINE" context WORKSPACE --source ID --section N --format json
```

2. Generate a coarse chronological contact sheet:

```bash
"$V2S_ENGINE" contact-sheet WORKSPACE --source ID --section N
```

Open the resulting image with Claude Code or Codex's native multimodal file viewer. Do not infer visual content from filenames or OCR alone.

3. If a consequential action, edit, animation, or state change remains unclear, extract only the relevant dense window:

```bash
"$V2S_ENGINE" frames WORKSPACE --source ID --from SECONDS --to SECONDS --fps FLOAT
```

Start around 0.5 fps for slides and 1-2 fps for coding, UI, or physical actions. Identify the minimum before/action/after evidence.

4. Store compact observations:

```bash
"$V2S_ENGINE" annotate WORKSPACE OBSERVATIONS_JSON
```

Each observation contains one falsifiable claim, `source_id`, start/end times, type, frame and transcript IDs, numeric confidence, observed/inferred/contradicted status, uncertainty, and producer identity.

5. Recompute gaps:

```bash
"$V2S_ENGINE" gaps WORKSPACE --source ID --format json
```

Prioritize missing evidence for procedure steps, verification conditions, safety warnings, exact code or UI details, central decision rules, and source conflicts. Ignore decorative or redundant visual gaps.

Use native host multimodal inspection before any hosted vision provider. Keep hosted vision disabled unless native viewing is unavailable or an authorized scale requirement justifies the privacy and usage cost.

### Evidence rules

- Speech can establish stated intent or explanation.
- A visible-state claim needs visual evidence.
- An action or transition normally needs temporally ordered before and after evidence.
- A successful procedure needs the demonstrated action and an observable result.
- Exact commands, labels, values, and code remain uncertain when not legible.
- Two probes that add no evidence are a stopping signal, not permission to guess.

Use high confidence for independently corroborated evidence, medium for one clear appropriate modality, and low for ambiguous or incomplete evidence. Never turn low-confidence exact details into authoritative steps.

## 6. Build the capability graph

Synthesize evidence into reusable records:

- Concept: operational meaning, prerequisites, use conditions, and evidence.
- Procedure: goal, inputs, ordered actions, state checks, recovery, and evidence per step.
- Decision rule: condition, choice, trade-off, and evidence.
- Demonstration: initial state, action, result, lesson, and evidence.
- Warning or anti-pattern: risk, consequence, correction, and evidence.
- Teaching inference: a new explanation, question, exercise, hint, or rubric clearly labeled as generator-created.

Each consequential record retains source ID, start and end time, modalities including temporal evidence when relevant, evidence IDs, observed-versus-inferred status, confidence, and uncertainty. Keep a presenter's claim distinct from the generator's pedagogical inference.

Normalize terminology only when equivalence is supported. Merge duplicate demonstrations, preserve meaningful variants, connect prerequisites, and retain attributed conflicts.

## 7. Generate the course-specific Skill

Derive a lowercase hyphenated name under 64 characters unless the user supplied one. The shareable package and raw workspace must be separate directory trees.

The course Skill must contain:

```text
<course-skill>/
├── SKILL.md
├── sources.md
├── provenance.json
├── chapters/          # semantic lessons, not one dump per video
├── exercises/         # generator-created practice
├── solutions/         # rubrics/answers loaded only after an attempt
├── playbooks/         # grounded application workflows
└── reference/         # precise terms, commands, states, and decisions
```

Add `learning-path.md`, `glossary.md`, `patterns.md`, `cheatsheet.md`, or indispensable cropped images only when the evidence justifies them. Never include raw video, audio, subtitles, complete transcripts, databases, cookies, caches, secrets, or decorative frame collections.

Generate the strict machine-readable authoring contract outside the evidence workspace before writing a blueprint:

```bash
"$V2S_ENGINE" blueprint-schema --workspace WORKSPACE --output AUTHORING_JSON
```

Read `AUTHORING_JSON` as an authoring envelope. Use its `blueprint_schema` to validate field shapes and copy `blueprint_seed` into a separate `BLUEPRINT_JSON`. Preserve the seed's `sources` and `coverage_ledger` exactly; complete only the semantic fields, artifacts, claims, principles, and limitations. Do not pass the authoring envelope itself to `build-skill`.

The resulting `CourseSkillBlueprint` separates structured sources, the workspace-bound coverage ledger, claims, artifacts, and core principles. Every mode needs at least one grounded artifact, Practice needs both an exercise and a separate solution or rubric, and every artifact needs a provenance claim. When `build-skill` receives `--workspace`, it must reject omitted, invented, retired, inaccessible, or failed course entries and any coverage upgrade that disagrees with the persisted workspace.

Assets are optional and minimal. Each selected image source must be a regular non-symlink file inside the declared workspace, be linked from a Markdown artifact, and have a visual or temporal provenance claim for that artifact. Give it a safe `assets/<name>.png` destination. The renderer rejects path escapes and unsafe sizes or formats, then decodes and re-encodes the image as metadata-free PNG; it never copies a raw frame byte-for-byte.

### Resident `SKILL.md`

Mark generated course Skills with:

```html
<!-- video-to-skill:course-skill:v1 -->
```

Keep the resident file concise and operational. It is a routing and teaching workflow, not a course summary. Put these sections in this order:

1. Scope and operating contract.
2. Learn, Practice, Apply, and Reference behavior.
3. Evidence and honest uncertainty rules.
4. A small set of evidence-linked core principles.
5. Learning, practice, application, and reference indexes.
6. Coverage limits and provenance pointers.

The four modes behave as follows:

- Learn diagnoses goals and prerequisite mastery one question at a time, chooses an adaptive next lesson, teaches one bounded concept with a grounded demonstration, checks retrieval or application, repairs misconceptions, and recommends the next step.
- Practice selects an appropriate exercise, presents success criteria without its solution, evaluates the learner's attempt with a rubric, gives the smallest useful hint, permits a retry, and reveals a solution only after an attempt or explicit request.
- Apply inspects the user's actual context, maps source assumptions to it, labels adaptations as inference, uses observable checkpoints, verifies results, and reports deviations from what the course demonstrated.
- Reference answers directly, loads only the smallest relevant file, preserves exact details only when supported, cites source timestamps, and distinguishes course evidence from outside knowledge.

Keep learner progress in the active conversation or host memory, not in the shareable Skill package.

### Supporting artifacts

Chapters follow semantic topics rather than video boundaries. Each includes objectives, prerequisites, core idea, demonstrated method, visual evidence, failure modes, a knowledge check, practice, and evidence.

Playbooks contain use conditions, preconditions, ordered steps, expected states, verification, recovery, limits, and evidence. Every authoritative numbered step needs a provenance claim.

Exercises and solutions are separate. Label exercises, hints, and rubrics as generator-created when the source did not contain them. Do not index solution files where the consuming agent might load them before an attempt.

Reference files favor compact decision tables, checklists, exact labels, state constraints, and timestamp pointers over repeated prose.

### Provenance

`provenance.json` uses schema version 1. Every consequential claim declares:

- stable ID, rendered file, kind, and compact derivative summary;
- `inferred` boolean and high/medium/low confidence;
- one or more evidence windows with `source_id`, numeric start/end, `modalities`, and `evidence_ids`.

Use modality values `speech`, `visual`, `ocr`, `metadata`, and `temporal`. The legacy singular `modality` remains readable, but new output uses the `modalities` list.

`sources.md` records each source's title, creator, platform, canonical URL when available, complete/partial/failed/skipped coverage, limitations, and timestamped claim map. Never expose private local paths.

## 8. Critique, build, validate, and install

Run an independent critic pass over the blueprint. Check that the planned package teaches and applies demonstrated capability rather than recapping narration; all four modes are actionable; solutions are withheld; source failures and uncertainty remain visible; each consequential claim uses the right modality; and no raw workspace artifact is planned for the package.

For a new full conversion, use the one internal build operation. It renders a new portable artifact, enforces workspace separation, validates the package and code fences, installs it into the active host, and returns the invocation:

```bash
"$V2S_ENGINE" build-skill BLUEPRINT_JSON --host claude --workspace WORKSPACE --output STAGED_SKILL
"$V2S_ENGINE" build-skill BLUEPRINT_JSON --host codex --workspace WORKSPACE --output STAGED_SKILL
```

Choose the current host without asking when it is known. Omit `--output` only when the default `./generated-skills/<name>` does not already exist. Add `--project` only when the user requested project-local installation; user scope is the default.

If build or validation fails, the command reports the error and retains any rendered artifact for inspection without installing it. Repair the blueprint, choose a fresh output path for the next attempt, and rerun the build. Use at most three repair cycles. Never claim success while errors remain.

For update/fold-in, keep the staged semantic-merge workflow. Validate the updated staging directory. Use the installer only for a fresh target name or an idempotent identical install:

```bash
"$V2S_ENGINE" validate STAGED_SKILL --check-code
"$V2S_ENGINE" install-generated STAGED_SKILL --host claude
"$V2S_ENGINE" install-generated STAGED_SKILL --host codex
```

The build operation and installer own discovery of the Claude Code or Codex Skill root, conflict detection, staging, validation, and atomic installation. They never overwrite different same-name content. For an actual update conflict, return the validated staged artifact and the exact conflict instead of claiming the installed version changed. Stop only for conflicting different content, ambiguous host, exhausted repair attempts, or a real permission failure.

## Completion report

Return the installed Skill name and path, its invocation form, workspace path, processed/partial/failed source counts, coverage and material limitations, files created, unresolved evidence gaps, critic repairs, and final validator result.

Show the next action as:

```text
/<course-skill> 从第一课开始教我          # Claude Code
$<course-skill> 帮我应用到当前项目        # Codex
```

Never make the user run an extraction, keyframe, annotation, validation, or installation command themselves.
