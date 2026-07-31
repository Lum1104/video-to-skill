# Architecture

## Boundary

The Python package is an evidence compiler and tool surface, not the final semantic author. It owns operations that benefit from determinism and recovery: acquisition, media decoding, caption selection, ASR, frame extraction, OCR, segmentation, caching, bounded retrieval, observation persistence, gap analysis, and validation. The host agent controls the semantic investigation loop and final generation by following the root `SKILL.md`.

This separation avoids binding skill generation to one text-model API while still allowing Claude, Codex, or another multimodal host to decide what evidence to inspect next. Provider interfaces remain available for headless ASR, OCR, and hosted-vision operation.

## Evidence workspace

Each workspace contains:

```text
manifest.json
coverage.json
evidence.sqlite3
logs/
  tool-runs.jsonl
sources/
  <source-id>/
    media.*
    captions.*
    audio-16khz.wav
    frames/
    investigation-frames/
    contact-sheets/
```

SQLite schema version 10 uses WAL mode and one connection per operation so multiple source workers and tool-run writers can commit safely. Supported older workspaces migrate forward when opened. Stage state is stored per source with a cache key containing the input identity, relevant configuration, and backend selection. A stage is reused only when its cache key still matches.

The database stores:

- ordered active source descriptors plus retired-source tombstones with removal time and reason
- per-input inspection reports with expected, accessible, inaccessible, and failed course entries
- stage state and failures
- transcript segments plus FTS5 text
- visual events, local frame paths, and `baseline` or `investigation` origin
- semantic timeline segments
- agent-authored observations grounded in transcript and frame IDs
- unresolved and resolved evidence gaps with suggested investigation windows
- stable logical tool executions with attempt, failure, cache-reuse, input, argument, and output-digest metadata
- structured warnings

Successful refreshed inspection tombstones a source missing from the latest inventory instead of deleting it or its evidence. Active queries omit retired sources by default, while the tombstone and retained transcripts, visuals, observations, and provenance references remain auditable.

Baseline visual events are the scene and periodic candidates produced by deterministic extraction. A successful visual refresh replaces only that source's baseline origin. Dense frames requested by the host are stored with investigation origin and survive baseline refreshes or later visual-stage failures; an existing visual ID cannot silently change origin.

Teaching-asset selection is a separate publication pipeline. Analyze proposes an evidence-grounded frame, normalized crop, or ordered two-to-four-frame sequence; deterministic code validates the frame IDs, decodes and composes the image, removes metadata, emits PNG, and records its digest. Author selects only from those immutable candidates and must link each selection from its consuming artifacts, while Review audits necessity, legibility, context, privacy, and on-demand loading. The generated Skill receives only selected sanitized images, never the baseline or dense-frame collections.

`coverage.json` schema version 2 aggregates the persisted inspection reports into a course-completeness proof, count totals, disclaimers, active source coverage, retired-source tombstones, and warnings. Completeness remains unproven when the expected count is unknown or any expected entry is inaccessible or failed.

The shared subprocess runner automatically records yt-dlp, FFmpeg, ffprobe, and other commands whenever an engine scope is active; ASR, OCR, vision, and retained investigation outputs use the same tracked-operation primitive because they do not all cross a subprocess boundary. SQLite is authoritative and uses one stable identity per tool version, operation, source, stage cache key, normalized arguments, and input-digest set. Every execution gets an immutable generation-numbered attempt, so a late completion cannot replace newer state and failure history remains inspectable; a reused stage increments the logical run's cache-hit count rather than emitting agent-written log chatter. ASR, OCR, and vision outputs identify typed workspace records and carry semantic digests that exports verify against SQLite. `tool-runs` exports records in stable ID order to canonical JSONL under `logs/`; no-follow traversal and atomic create-only publication accept an existing file only when its bytes are identical. It stores no raw output, arbitrary environment, credentials, signed URLs, or host-private absolute paths, and the raw tool history is never part of the generated Skill.

`manifest.json` also owns the analysis-depth contract. Inspection deterministically summarizes duration, active and expected item counts, chapters, caption coverage, course structure, and visible content signals. `auto` resolves to `standard` or `deep`; `archival` is explicit because its storage and review boundary is material. The versioned budget summary controls frame cadence, scene sensitivity, width, perceptual deduplication, duration-scaled frame retention, semantic segment size, Analyze packet limits and fanout, and investigation affordances. Absolute course, media, packet, frame, and investigation maxima remain hard safety caps.

The contract is resolved before affected extraction stages, included in their cache keys, and copied into Analyze packets, `analysis/run-config.json`, workspace compiler receipts, and completion records. Resume reuses it and rejects a conflicting requested depth, corrupted digest, or profile-version drift. `--refresh` is the only normal path that recomputes it after inspectable inventory or density changes. A legacy workspace receives a marked compatibility contract before new analysis tasks are snapshotted.

Depth never changes `visual_profile`, ASR/OCR/vision providers, API-key requirements, authentication, network authorization, or redistribution policy. In particular, `visual_profile=transcript` produces zero visual-extraction budgets at every depth, and a deeper profile never enables hosted vision.

Media and frames are private, reproducible cache artifacts. `clean` removes those files but not the database, captions, manifests, or generated skill.

## Source lifecycle

1. `inspect` resolves a URL or local file into accessible `SourceDescriptor` records and a completeness report that accounts for expected, inaccessible, and failed entries.
2. `acquire` downloads the smallest required representation and caption tracks.
3. `transcribe` scores manual/automatic captions, falling back to ASR when needed.
4. `visuals` extracts scene and periodic candidates, deduplicates them, runs OCR, and conditionally invokes a vision provider.
5. `segment` combines creator chapters, pauses, lexical topic shifts, and visual changes while enforcing a maximum segment duration.
6. The host agent reviews a bounded contact sheet and matching context for one segment at a time.
7. When an action or state transition is ambiguous, the agent requests a short dense-frame window and records a structured observation tied to the exact source evidence.
8. `gaps` deterministically audits visual coverage and grounding; the agent repeats the investigation only for actionable gaps and stops at the configured review budget.

Course items run concurrently up to `max_workers`. Failure of one item is recorded and does not discard successful evidence from the rest of the course.

## Agent control loop

The host agent is responsible for planning and replanning, while the CLI supplies bounded tools and persistent state:

```text
inventory
  → section context + contact sheet
  → native multimodal inspection
  → optional dense temporal zoom
  → grounded observation
  → evidence-gap audit
  → repeat or generate
```

Contact sheets and dense frames are private, reproducible cache artifacts. Observations contain concise factual conclusions and evidence references, never hidden model reasoning. Before publication, the host turns the inspected evidence into a canonical semantic map: source-backed concepts, procedures, examples, decisions, cautions, and demonstrations, plus their relationships and publication dispositions.

## Extension points

`SourceAdapter`, `Transcriber`, `OCRProvider`, and `VisionAnalyzer` are narrow abstract interfaces. Source adapters are also exposed through the `video_to_skill.source_adapters` Python entry-point group. An adapter must:

- reject unsupported locators without network access;
- return stable source IDs during inspection;
- materialize only inside the supplied source directory;
- never place secrets in its descriptor or returned paths;
- provide a content hash that changes when reusable evidence changes.

Provider implementations must return typed models and raise domain errors with actionable messages. The pipeline treats unexpected provider exceptions as isolated source/stage failures.

## Generated skill boundary

The complete V2 product contract and its design rationale live in [generated-skill-v2.md](generated-skill-v2.md). This section summarizes the implementation boundary.

Before semantic authoring, `blueprint-schema --workspace` emits the strict V2 Pydantic schema and a sanitized seed containing the workspace's exact active, retired, inaccessible, and failed course ledger. The seed contains digests and public metadata rather than local locators, raw failure text, transcripts, frames, or media. The author completes:

- a high-recall semantic map whose material units are included, merged, or omitted with an explicit reason;
- semantic relations and source-level evidence links;
- a capability profile for teaching, practice, application, and reference behavior;
- one recommended curriculum plus optional alternate paths over the same semantic map;
- an evidence-driven artifact plan in which every separate file states why independent loading is useful.

`build-skill --workspace` recomputes the ledger and rejects omissions, additions, metadata changes, stale workspace identity, unsupported coverage upgrades, invalid semantic links, and material semantic units that silently disappear. It does not enforce a fixed chapter count or an artifact quota per interaction mode.

The generated Skill is a derivative distribution artifact. It receives selected keyframes and synthesized Markdown, but never the workspace database, raw video/audio, subtitle files, or complete transcript. Its stable contract is:

```text
SKILL.md
source-map.md
sources.md
provenance.json
build-manifest.json
[evidence-driven Markdown collections]
[selected assets]
```

`SKILL.md` is an adaptive front door rather than a mode menu. An empty invocation gives a short welcome and offers `start` without creating files or launching a workflow. A substantive request may ask up to three useful context questions, then teaches, coaches practice without exposing answers early, applies the material to the user's situation, or retrieves a precise reference according to intent. It follows the user's language and distinguishes source-grounded content, explicit inference, and outside or current knowledge.

Artifact language is a separate immutable build contract. New runs persist either an explicit language/locale or `source`; uniform known source evidence resolves deterministically, while mixed or unknown evidence receives one constrained curriculum-agent declaration. Author and repair cannot change it, Review sees both the contract and declaration, and compilation revalidates them. Generated Skill interaction still follows the learner's language by default.

Named publication editions make that build contract edition-local without duplicating evidence. `edition WORKSPACE NAME` creates an immutable view under `editions/<deterministic-edition-id>/`: its run configuration, language contract, tasks, behavior targets, build receipts, completion, and all post-Analyze canonical heads are isolated. Shared source and Analyze records remain under the legacy workspace namespace. SQLite keeps its compatible generic record schema; edition heads encode the edition ID in their logical record ID, and every downstream task scope carries that ID. There is no global active-edition pointer.

An edition pins one integrated Analyze producer, workspace/source snapshot, every canonical Analyze digest, and the analysis-depth contract digest. Existing curriculum paths are bound by a completed deterministic `curriculum-reuse` task, so no planning agent runs. `--plan-curriculum` creates one bounded curriculum task over the pinned Analyze records. Both routes then require a fresh full Author, isolated behavior trials, independent Review, compilation, validation, and no-clobber installation. A changed source/depth snapshot or canonical Analyze head makes the lineage stale and requires a new edition.

The legacy unnamespaced downstream heads remain the compatibility edition for current single-edition workspaces and ordinary `run` resume. New named editions neither migrate nor move those heads. A localized same-curriculum edition compares logical artifact IDs, claim IDs, semantic bindings, evidence IDs, and timestamps with its source baseline. Drift is rejected unless immutable edition metadata records an explicit justification. Selecting a different path or explicitly requesting a new plan records the structural reason. This is edition lineage, not update lineage, so `parent_build_id` stays reserved and unset.

`source-map.md` exposes what the source covers and how the generated curriculum relates to it. `sources.md` preserves verified course accounting. `provenance.json` connects rendered claims and semantic units to workspace source IDs, times, modalities, and evidence IDs without copying transcript text. `build-manifest.json` records the deterministic build identity, source and workspace digests, curriculum choice, managed-file hashes, and optional parent build so later updates can distinguish regenerated content from human edits. A build without a workspace is explicitly marked unverified and cannot retain a self-asserted coverage ledger.

For a named edition, `build-manifest.json` additionally carries the path-free edition ID/name, configuration and Analyze-lineage digests, integrated Analyze task ID, shared canonical Analyze digests, source/depth snapshot proofs, curriculum source/path identity, and any identity-drift justification. Raw evidence remains outside the portable Skill.
