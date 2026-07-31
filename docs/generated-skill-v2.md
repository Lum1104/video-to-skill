# Generated Skill V2

## Status

This document is the product and technical contract for the implemented V2 `video-to-skill` generation pipeline. It replaces the earlier generated-course contract rather than maintaining two blueprint versions.

The motivating example is the generated Skill derived from Sam Altman's 39-minute Y Combinator interview, _Never a Better Time to Do a Startup_. The source contains many arguments, examples, qualifications, predictions, and unresolved questions, but the first generated package reduced it to a small number of principles spread across several relatively thin files. The evidence workspace that made the build possible was also invisible from the published Skill.

V2 addresses both failures and the later product audit:

1. preserve substantially more source meaning before designing a course;
2. preserve instructional affordances independently from semantic coverage; and
3. preserve a private, reproducible source project without turning the installed Skill or Git repository into a raw-media archive.

## Decisions

The following decisions are part of this contract.

- Use a thematic course as the default primary curriculum.
- Preserve source-faithful and application-first paths over the same canonical semantic map when the material supports them.
- Set no fixed chapter count. Split by semantic independence and learning value.
- Treat learn, practice, apply, and reference as behaviors with relative capability levels, not as four required directories or artifact quotas.
- Give every artifact an independent loading reason. Merge artifacts that are always loaded together.
- On empty invocation, provide a short, inviting, course-specific welcome and offer `start`.
- After the learner starts, ask at most three short questions when context is genuinely missing.
- Keep one canonical artifact language per build and answer in the learner's language by default.
- Preserve the full evidence workspace locally by default.
- Keep raw media out of the installed Skill and ordinary Git history.
- Include a readable source map, machine provenance, and a portable build manifest in every generated Skill.
- Validate semantic retention and runtime behavior, not only file structure.
- Never silently overwrite a human-edited generated Skill.
- Keep the engine model-agnostic and free of direct LLM calls.
- Use the durable workspace as the data plane for task packets, results, drafts, reports, and canonical revisions.
- Use `Analyze → Author → Review` as the three reasoning boundaries.
- Use `run` and `submit` as the normal public orchestration protocol.
- Compile the strict blueprint from canonical workspace state rather than asking the main agent to author or transport it.
- Track instructional-affordance coverage separately from semantic coverage.

## Product model

The product has three durable layers.

### 1. Evidence workspace

The private evidence workspace is the source project. It is resumable, queryable, and suitable for later investigation or regeneration.

It may contain:

```text
workspace/
├── manifest.json
├── coverage.json
├── evidence.sqlite3
├── sources/
│   └── <source-id>/
│       ├── metadata.json
│       ├── media.*
│       ├── audio-16khz.wav
│       ├── captions.*
│       ├── frames/
│       ├── investigation-frames/
│       └── contact-sheets/
├── observations/
│   └── observations.jsonl
├── analysis/
│   ├── semantic-map.json
│   ├── semantic-relations.json
│   ├── capability-profile.json
│   ├── gaps.json
│   └── semantic-coverage.json
├── design/
│   ├── curriculum-options.json
│   ├── selected-curriculum.json
│   └── artifact-plan.json
├── builds/
│   └── <build-id>/
│       ├── blueprint.json
│       ├── critic-report.json
│       ├── validation-report.json
│       └── behavior-report.json
└── logs/
    └── tool-runs.jsonl
```

The default lifecycle keeps this workspace. `clean` remains an explicit user action. Completion reports must show the workspace path and retention state.

### 2. Canonical semantic map

The canonical semantic map is a high-recall, source-ordered representation of meaning. It sits between raw evidence and curriculum design.

It is not:

- a complete transcript;
- a five-point summary;
- a chapter outline; or
- a list of only immediately actionable claims.

It preserves important questions, claims, reasons, examples, analogies, definitions, distinctions, qualifications, counterpoints, predictions, recommendations, warnings, value judgments, and open questions.

### 3. Generated Skill

The generated Skill is a portable knowledge product. It contains the operating contract and the smallest set of independently useful course artifacts.

Every V2 package contains five fixed root records and at least one authored Markdown artifact. A representative evidence-justified package can contain:

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

The five fixed root records are unconditional. The remaining entries are supported artifact shapes, not required directories: `chapters/` holds independently loadable teaching units, `exercises/` holds practice, `solutions/` holds after-attempt answers and answer-bearing rubrics, `playbooks/` holds operational application, `reference/` and the allowed root reference files support fast lookup, and `assets/` holds only indispensable images.

Content, learning paths, applications, exercises, solutions, references, and assets are driven by semantic coverage, capability ceilings, instructional-affordance coverage, and independent loading boundaries. “Optional” means “not manufactured when unsupported,” not “prefer a thin package.”

## End-to-end workflow

```text
inspect sources
  → acquire and persist evidence
  → segment by semantic and visual events
  → investigate unresolved evidence
  → build a high-recall semantic map
  → link semantic relationships
  → audit semantic coverage
  → propose curriculum designs
  → select a primary curriculum
  → plan justified artifacts
  → render a staged Skill
  → run structural and semantic validation
  → run independent behavior tests
  → install without clobbering
```

Do not combine high-recall extraction, importance ranking, deduplication, and curriculum writing into one authoring pass. Early compression causes permanent information loss.

## Evidence acquisition and retention

### Preserve versus publish

Saving evidence does not imply installing or publishing it.

Default policy:

```text
Local workspace
  Keep complete analysis evidence.

Installed Skill
  Include semantic derivatives, provenance, selected teaching assets, and a
  build receipt.

Git repository
  Commit the Skill, not raw video or full analysis media.

Release artifact
  Optionally publish a compact evidence bundle when appropriate.
```

Never retain cookies, authorization headers, temporary tokens, private absolute paths, or unrelated private screen content in a shareable artifact.

### Sanitized tool records

Save reproducibility metadata for deterministic media operations:

```json
{
  "tool": "ffmpeg",
  "version": "7.1",
  "operation": "extract-dense-window",
  "source_id": "youtube-ZIaOBAjvc38",
  "input_sha256": "…",
  "arguments": ["-ss", "762", "-to", "810", "-vf", "fps=2"],
  "outputs": ["sources/youtube-ZIaOBAjvc38/investigation-frames/…"]
}
```

Records use workspace-relative paths and sanitized arguments. They never include credentials or expiring source URLs.

### Evidence retention levels

#### Keep locally

Default. Preserve the complete workspace for later criticism, redesign, or regeneration.

#### Compact portable bundle

Create an optional archive containing:

- source metadata;
- captions and normalized transcript when redistribution is appropriate;
- semantic map and relations;
- selected keyframes and contact sheets;
- observations and gaps;
- curriculum designs and blueprint;
- critic and validation reports; and
- sanitized tool records.

Do not include complete source video or audio.

#### Archival local bundle

Create an optional private archive that may additionally include analysis-quality media, audio, all extracted frames, and the evidence database.

The archive is not a normal Skill dependency and is not committed to ordinary Git history.

## Adaptive analysis depth

Fixed investigation limits are safety valves, not quality targets. A 15-minute interview and a three-hour coding course must not receive the same absolute visual or semantic budget.

Route evidence per semantic section:

| Section type | Primary evidence | Investigation emphasis |
| --- | --- | --- |
| Interview or Q&A | speech, speaker turns, surrounding question | arguments, examples, qualifications, corrections |
| Slides or lecture | speech, slide text, diagrams | slide transitions, chart interpretation, evolving diagrams |
| Coding | spoken intent, legible code, commands, results | before/edit/run/after states, errors, fixes |
| Software or UI | labels, inputs, navigation, saved state | before/action/after transitions |
| Physical procedure | objects, hands, tools, ordered states | action sequence, safety, final condition |
| Mixed | section-specific | route each section independently |

Allocate investigation work from:

- topic changes;
- speaker changes;
- chapter boundaries;
- scene and slide changes;
- code or UI activity bursts;
- dense numeric or technical language;
- phrases likely to introduce qualifications;
- demonstrations starting or ending; and
- unresolved semantic or visual gaps.

Stop when material evidence gaps are closed or honestly marked partial. Do not claim completeness merely because a fixed number of review passes has run.

If a safety limit is reached while material gaps remain:

1. try a lower-cost evidence route;
2. mark the affected coverage partial; or
3. ask to expand the budget only when the extra time, storage, paid service, or privacy boundary is material.

For product-level control, expose `standard`, `deep`, and `archival` analysis depth rather than FPS, OCR thresholds, or FFmpeg flags. Recommend the level from source density and publishing intent.

## Canonical semantic map

### Four analysis layers

#### Evidence segment

The closest bounded pointer to source material:

```json
{
  "id": "segment-012",
  "source_id": "youtube-ZIaOBAjvc38",
  "start": 762.4,
  "end": 791.8,
  "speaker": "Sam Altman",
  "transcript_ids": ["tx-182", "tx-183"],
  "frame_ids": ["frame-0762"],
  "caption_confidence": "medium"
}
```

#### Semantic unit

One meaningful element derived from evidence:

```json
{
  "id": "unit-conviction-updates",
  "source_id": "youtube-ZIaOBAjvc38",
  "start": 762.4,
  "end": 791.8,
  "speaker": "Sam Altman",
  "kind": "claim",
  "summary": "Useful conviction continues updating with new evidence.",
  "materiality": "core",
  "disposition": "included",
  "inferred": false,
  "confidence": "medium",
  "modalities": ["speech"],
  "evidence_ids": ["tx-182", "tx-183"]
}
```

Allowed kinds:

```text
question
claim
reason
example
analogy
definition
distinction
qualification
counterpoint
prediction
recommendation
warning
value-judgment
open-question
```

Materiality:

```text
core
supporting
contextual
incidental
```

Disposition:

```text
included
merged
context-only
omitted
```

Every non-included unit records a reason. A merged unit also identifies the stable unit into which its curriculum presentation was merged.

#### Semantic relation

Relations preserve argument structure:

```text
answers
supports
explains
exemplifies
qualifies
contrasts-with
depends-on
updates
contradicts
raises
leaves-unresolved
```

Example:

```text
“AI makes implementation easier”
    supports
“Founders should raise the ambition ceiling”

“Tool fluency spreads quickly”
    qualifies
“Using AI tools alone creates a durable moat”

“Maintain conviction”
    constrained through qualifies
“Continue updating with evidence”
```

#### Curriculum view

Curricula select and order semantic units without deleting or mutating the map. Deduplicate presentation here, not in evidence or semantic storage.

### High-recall passes

1. **Extraction:** find meaningful units without merging or prematurely ranking them for teaching.
2. **Normalization:** normalize terminology and connect related units while retaining source-specific IDs.
3. **Materiality review:** mark importance and disposition without silently deleting long-tail context.
4. **Curriculum design:** organize views over the complete map.

### Human-readable source map

Render `source-map.md` chronologically:

```markdown
## 12:43–13:12 · Conviction must update

**Question:** What makes a non-consensus belief useful?

**Claim:** Disagreement alone is not evidence of correctness.

**Reasoning:** Conviction remains useful only while it incorporates new evidence.

**Distinction:** This separates conviction from stubbornness.

**Limit:** The interview does not define a sufficient-evidence threshold.

**Connections:** Acting before certainty; founder feedback networks.
```

This file is a high-density reference layer, not a transcript dump.

## Coverage

Report two independent concepts.

### Source-acquisition coverage

Whether the complete input inventory was inspected, acquired, transcribed, and processed.

### Semantic coverage

Whether every material unit has an explicit disposition and whether included material is represented in at least one course artifact.

Example:

```text
Source acquisition: complete
Material semantic units: 31
Represented in course: 25
Reference-only/context: 4
Intentionally omitted: 2
Unaccounted: 0
```

Never use a single `complete` label for both concepts.

### Instructional-affordance coverage

Semantic coverage proves that source meaning survived. It does not prove that the generated Skill retained the product surfaces needed to teach, practice, apply, give feedback, recover from failure, or answer quick operational questions.

Track a separate instructional-affordance ledger for:

- learning objectives and misconceptions;
- retrieval and transfer prompts;
- focused exercises and success criteria;
- scored rubrics, progressive hints, retry, and capstone synthesis;
- operational playbooks, expected output states, validation, and recovery; and
- quick reference and decision rules.

Every entry is `provided`, `unsupported`, or `not-applicable` with semantic-unit links and a rationale. Strong capability claims require the complete corresponding surface; weaker levels require proportionally smaller surfaces.

This is not an artifact quota. Several affordances may share one independently useful artifact, and one affordance may span several justified artifacts.

## Curriculum design

### No fixed chapter count

A chapter is justified when it has:

- one clear learning objective;
- a coherent cluster of semantic units;
- independent learning or application value;
- a way to check understanding; and
- an independent loading reason.

Merge sections that must always be understood together. Split sections with different prerequisites, application contexts, or central tensions.

Do not shorten a course merely to hit a target count, and do not fragment it to look comprehensive.

### Curriculum checkpoint

After the semantic map is complete and before artifact authoring, propose two or three materially different designs when justified. Ask about the desired experience, not a number of chapters.

For the motivating interview, reasonable designs are:

#### A. Source-faithful companion

Follow the interview's question and answer sequence. Preserve the rhetorical arc, historical examples, qualifications, forecasts, and transitions. Best for watch-along learning and precise source reference.

#### B. Thematic founder course — default

Reorganize material by conceptual dependency:

```text
How AI changes the ambition ceiling
Where startup advantage actually comes from
What AI does not replace
Conviction versus delusion
Updating beliefs with evidence
Choosing cofounders and compounding relationships
Acting when the path is incomplete
Safety as a product constraint
Distributed power and human agency
Building a founder operating system
```

The semantic structure, not this example list, determines the final count.

#### C. Application-first operating system

Organize around actual decisions:

```text
Opportunity review
Ambition review
Moat review
Conviction review
Cofounder and network review
First-step design
Safety-floor review
Power-concentration review
```

This path adapts conceptual claims and must label those adaptations as inference.

### Multiple paths, one knowledge base

The selected curriculum is the primary path. Alternate watch-along and application-first paths may reference the same artifacts or semantic units without duplicating knowledge.

```text
Canonical semantic map
├── Thematic course — recommended
├── Watch-along path
└── Application-first path
```

Changing the selected curriculum later should not require reacquiring or reanalyzing the video.

## Artifact design

Create a file only when at least one condition holds:

1. users may request it independently;
2. it is rarely needed and should not consume normal context;
3. it must be withheld, such as a solution;
4. it is large enough to benefit from progressive disclosure;
5. it represents a distinct workflow; or
6. it uses a machine-readable format.

Every artifact records:

- stable artifact ID;
- supported behaviors;
- disclosure policy (`normal` or `after-attempt`);
- when to load it;
- independent loading reason;
- semantic unit IDs;
- topics;
- content; and
- provenance claims.

If no independent loading reason exists, merge it with the nearest artifact.

Possible package sizes:

```text
Compact
  SKILL.md
  source-map.md
  sources.md
  provenance.json
  build-manifest.json
  learning-path.md or another allowed root artifact

Standard
  SKILL.md
  source-map.md
  sources.md
  provenance.json
  build-manifest.json
  chapters/
  exercises/ and after-attempt solutions/ when practice is supported
  playbooks/ when operational application is supported
  reference/ or an allowed root reference artifact when fast lookup is supported
  indispensable assets/ when visual evidence needs to travel with the Skill

Large course
  Multiple independently useful content, practice, application, and reference artifacts justified by the semantic and learning structure.
```

Do not require optional directories or target a file count. Exercises with answers always keep solutions separate with an explicit `after-attempt` disclosure policy. The renderer uses that policy, not a directory name, to keep them unindexed until an attempt or explicit request.

## Capability profile

Learn, practice, apply, and reference are routing behaviors. Assign each a relative level:

```text
strong
medium
light
unsupported
```

Example for a conceptual interview:

```text
Learn       strong
Reference   strong
Practice    medium · generator-created
Apply       medium · adapted from conceptual claims
```

Example for a coding tutorial:

```text
Learn       medium
Reference   strong
Practice    strong
Apply       strong
```

Do not manufacture a playbook, exercise, or reference file to make the profile symmetrical. The runtime still understands all four intents and honestly states when the source provides only light or unsupported coverage.

The same semantic unit may support several behaviors:

```text
Evidence-updated conviction
├── Learn: explain conviction versus stubbornness
├── Practice: evaluate a founder case
├── Apply: inspect a user's disconfirming evidence
└── Reference: retrieve the exact source window
```

## Triggering and empty invocation

### Trigger boundary

Frontmatter describes the source or its distinctive framework, not an entire broad domain.

Should trigger:

```text
What did Sam mean by non-consensus conviction in the YC interview?
Use this interview's framework to examine my AI startup.
Review this product through safety, distributed power, and human agency.
```

Should not trigger solely because of:

```text
I have an AI startup idea.
How should I price SaaS?
Help me write a fundraising deck.
```

The cost of a broad trigger is not only extra context. It silently constrains a generic question to one source's worldview.

### Empty invocation

When the user invokes the Skill without a request:

1. load no supporting artifact;
2. inspect no project;
3. run no command;
4. create no file;
5. respond in no more than two short sentences;
6. state one inviting course-specific outcome;
7. invite the user to share context or say `start`; and
8. wait.

Do not show:

- a mode menu;
- prerequisites;
- a syllabus;
- evidence or provenance terminology;
- limitations;
- a full capability list; or
- evaluation language such as "stress test" unless the user asks for it.

Example:

> Let's explore what your AI startup could become. Tell me what you're working on—or just say "start" and I'll guide you.

### Initial context

After the user chooses to begin, ask at most three short questions in one turn when context is missing. Tell the user short answers are enough.

Generic thematic-course example:

1. What problem do you want AI to solve?
2. Who feels this problem most strongly?
3. If solving it became dramatically easier, what larger outcome could become possible?

Application example:

1. What are you building?
2. What evidence currently makes you believe it should exist?
3. What part of the thesis are you least certain about?

Three is a maximum, not a target. Ask fewer questions when the user already provided context. Ask no intake questions for a precise reference request.

## Teaching interaction

Chapters are evidence-backed teaching material, not response scripts.

Default loop:

```text
understand the smallest necessary context
  → introduce one useful cognitive move
  → ground it in one source example when useful
  → ask one transfer or retrieval question
  → adapt the next step
```

Do not automatically announce a lesson number, display learning objectives, or dump a chapter.

Example:

```text
User:
Small companies cannot afford good legal support.

Skill:
That is a useful starting point. The first move in this framework is to separate
"making legal work cheaper" from the larger outcome that cheaper implementation
could unlock. If routine legal work became 10× cheaper, what could a small
company safely do that it avoids doing today?
```

Default interaction should:

- introduce one main concept per turn;
- ask at most one next-step question after the initial context packet;
- avoid turning every response into a formal quiz;
- skip questions the user has already answered;
- answer precise questions directly;
- let the user change direction at any time; and
- increase depth only when the user asks or demonstrates readiness.

Artifacts may contain:

- core idea;
- source reasoning;
- examples;
- qualifications;
- common misconceptions;
- diagnostic prompts;
- transfer prompts; and
- deepening prompts.

The Agent selects what is useful; it does not emit the template.

Practice feedback should normally be conversational rather than a scorecard. Use a strict rubric display only when requested.

Application should begin with the highest-information missing question, not a long intake form.

## Knowledge boundaries

Use three knowledge layers.

### Source-grounded

Material explicitly stated or demonstrated in the source. Consequential claims retain timestamps and evidence.

### Teaching or application inference

Exercises, explanations, analogies, and adaptations created from source principles. Mark these naturally:

```text
Applied to your situation…
One way to adapt this idea is…
This suggests, but does not establish, that…
```

Do not imply that the presenter discussed the user's case.

### Outside or current knowledge

Current market data, laws, competitors, later events, and general domain knowledge are not course evidence.

Use them when the request calls for them, while keeping the boundary clear. Ask permission only when continuing requires a material external action, private data, paid access, or the user explicitly requested source-only reasoning.

Do not force every normal answer into visibly labeled sections. Use explicit labels only when mixing layers could mislead the user or when an audit is requested.

Time-sensitive source claims always retain their original context. A question about whether they remain true requires current evidence rather than silently upgrading the source.

## Language

Track:

```text
Source language
Artifact language
Interaction language
```

Use one canonical artifact language per build. Respond in the user's language unless requested otherwise.

For ordinary generation, infer artifact language from the user. For a public, international release, recommend English unless the user specifies another audience.

Do not duplicate every artifact merely to support multilingual interaction. When a separately published localized edition is needed, render it from the same semantic map with stable semantic-unit, claim, artifact, and timestamp IDs.

Preserve original proper names and technical terms. Distinguish paraphrase from short source quotation, and never turn uncertain captions into confident text through translation.

## Visual evidence and teaching assets

Raw visual evidence belongs in the workspace. Indispensable teaching visuals belong in the Skill.

Include a visual asset only when text cannot reliably preserve an important visual or temporal claim:

- a meaningful chart or slide;
- a diagram;
- a before/action/after UI transition;
- a legible code-state change;
- an important physical-procedure state; or
- a safety-critical visual condition.

Do not require a minimum or fixed maximum per chapter. A pure interview may need no course visual, while a UI tutorial may require many.

Every asset records:

- stable asset ID;
- source window;
- role;
- supported claims;
- source frames;
- crop, composition, or re-encoding transformation;
- accessible description; and
- why text is insufficient.

Static state may use one crop. A transition normally requires ordered before/action/after evidence, commonly rendered as a portable contact strip.

Prefer recoverable text for code when it is legible and accurate. Use a screenshot when visual state is part of the evidence or exact text remains uncertain.

Validation checks that the asset is linked, legible enough for its claim, correctly ordered, grounded in visual or temporal evidence, non-decorative, and free of unrelated private information.

## Build manifest and reproducibility

Every package includes `build-manifest.json`:

```json
{
  "schema_version": 2,
  "build_id": "v2s-…",
  "parent_build_id": null,
  "generator": {
    "name": "video-to-skill",
    "version": "…"
  },
  "artifact_language": "English",
  "curriculum": {
    "selected_path_id": "thematic",
    "selected_kind": "thematic"
  },
  "source_snapshot_digest": "…",
  "workspace_snapshot_digest": "…",
  "semantic_map_digest": "…",
  "managed_files": {
    "SKILL.md": {
      "sha256": "…"
    },
    "content/conviction.md": {
      "artifact_id": "artifact-conviction",
      "sha256": "…"
    }
  }
}
```

The manifest never exposes a local workspace path. It gives later builds enough information to identify generated ownership and source lineage.

## Future update safety

Update and fold-in are deliberately outside the first workspace-centered implementation. The following remains the target contract for a future independently designed update workflow; no compatibility with the former host-authored blueprint path is implied.

A generated Skill may become an independently maintained open-source project. Regeneration must preserve human work.

Compare:

1. hashes from the previous build manifest;
2. current, possibly human-edited files; and
3. the new staged build.

Rules:

```text
Current hash equals previous generated hash
  Safe to replace in a staged update.

Current hash differs
  Human-edited; do not overwrite.

Current file is absent from the previous manifest
  User-managed; preserve.

New build omits a previously generated file
  Do not delete until semantic migration is reviewed.
```

Stable semantic-unit, relation, artifact, claim, exercise, solution, and learning-path IDs make file renames and chapter splits understandable.

Example update report:

```text
Semantic units
+ 12 newly represented
~ 4 expanded
- 0 lost

Artifacts
+ 5 new
~ 6 safely regenerated
! 2 contain human edits
= 3 unchanged
```

Render updates to a new staging directory. Carry safely attributable human material forward. Retain unresolved conflicts for review. Never overwrite the installed Skill.

README and repository presentation files are user-managed unless a separate publishing workflow explicitly owns them.

## Validation

### Structural and security validation

Check:

- valid Skill frontmatter and name;
- safe paths and links;
- no raw or private workspace artifacts;
- no secrets or private paths;
- valid provenance and build-manifest schema;
- grounded claims;
- valid evidence windows and modalities;
- solution withholding;
- asset safety; and
- generated-file hashes.

### Semantic coverage validation

Check:

- unique stable semantic IDs;
- valid source and relation references;
- a disposition for every unit;
- reasons for merged, context-only, and omitted units;
- representation of every included core or supporting unit;
- semantic links from artifacts and claims;
- an independent loading reason for every artifact;
- no artifact created only to fill a behavior quota; and
- separate source-acquisition and semantic-coverage reporting.

### Host-neutral behavior validation

Use realistic prompts in a fresh context:

```text
$skill
  Short welcome, no artifact loading, then wait.

$skill start
  Ask one to three low-friction, course-specific questions.

Teach me from the beginning.
  Introduce one bounded idea and adapt.

Give me an exercise.
  Withhold the solution until an attempt.

Apply this to my project.
  Inspect missing real context before adapting.

What did the speaker say about X?
  Answer first and cite the source window.

Help me with a generic out-of-scope task.
  Do not pretend the source covers it.
```

### Content pressure tests

Sample across the semantic map:

- an opening thesis;
- a middle example;
- a qualification;
- a likely misconception;
- a time-sensitive prediction;
- an unresolved question; and
- a visually grounded or temporal claim when present.

Verify that the Skill finds the right material, preserves qualifications, distinguishes source from inference, and admits missing evidence.

### Completion report

Do not return only `Validation passed`.

Example:

```text
Structure              PASS
Source acquisition     complete
Semantic coverage      PASS · 31/31 material units accounted for
Empty invocation       PASS
Learning behavior      PASS
Practice withholding   PASS
Application behavior   PASS
Reference grounding    PASS
Scope boundary         PASS
Evidence retention     kept locally
Build manifest         PASS
```

Deterministic validation owns structural and semantic checks. Independent behavior tests use a fresh context and save raw reports in the workspace, not in the installed Skill.

## Workspace compilation contract

The host agent no longer authors or transports a monolithic blueprint.

`run` materializes bounded task directories. Analyze, Author, and Review workers read their own packets, write their own strict result files, and call `submit` directly. SQLite stores task identity, dependencies, leases, results, immutable canonical revisions, and canonical heads. Large semantic records and Markdown drafts remain workspace files.

After an independent Review passes, deterministic compilation assembles the strict in-memory blueprint from:

- workspace-derived source inventory and acquisition ledger;
- canonical semantic units and relations;
- canonical capability profile and instructional-affordance ledger;
- selected curriculum and interaction contract;
- artifact specifications and verified draft files;
- claims, evidence links, assets, and limitations; and
- passing critic and behavior reports.

The workspace build receipt stores artifact paths and digests rather than embedded Markdown bodies.

The compiler and renderer reject:

- missing or unknown semantic references;
- duplicate semantic, relation, path, artifact, affordance, or claim IDs;
- included core or supporting units absent from course artifacts;
- capability claims that exceed Analyze ceilings or lack required affordances;
- curriculum paths that expose after-attempt material;
- artifacts without independent loading reasons, semantic links, or verified drafts;
- stale task snapshots, invalid leases, or out-of-packet evidence references;
- artifacts without provenance;
- unknown source evidence;
- visual assets without linked visual or temporal evidence;
- a blueprint that changes persisted workspace inventory; and
- compilation without passing independent critic and behavior reports.

The implemented sequence is:

1. `run` inspects, acquires, transcribes, analyzes visuals, segments sources, and creates bounded Analyze work.
2. Analyze submissions produce the canonical semantic map, relations, conflicts, coverage, and capability ceilings.
3. Author submissions produce curriculum, interaction, artifact plans, instructional-affordance coverage, claims, and immutable drafts.
4. Review submissions independently audit semantic and product retention plus runtime behavior.
5. Failed reviews create immutable Author repair tasks and fresh Reviews, with at most three cycles.
6. Passing state compiles, renders outside the workspace, validates, installs without clobbering, and persists a completion record.
