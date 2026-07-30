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
sources/
  <source-id>/
    media.*
    captions.*
    audio-16khz.wav
    frames/
    investigation-frames/
    contact-sheets/
```

SQLite schema version 4 uses WAL mode and one connection per operation so multiple source workers can commit safely. Supported older workspaces migrate forward when opened. Stage state is stored per source with a cache key containing the input identity, relevant configuration, and backend selection. A stage is reused only when its cache key still matches.

The database stores:

- ordered active source descriptors plus retired-source tombstones with removal time and reason
- per-input inspection reports with expected, accessible, inaccessible, and failed course entries
- stage state and failures
- transcript segments plus FTS5 text
- visual events, local frame paths, and `baseline` or `investigation` origin
- semantic timeline segments
- agent-authored observations grounded in transcript and frame IDs
- unresolved and resolved evidence gaps with suggested investigation windows
- structured warnings

Successful refreshed inspection tombstones a source missing from the latest inventory instead of deleting it or its evidence. Active queries omit retired sources by default, while the tombstone and retained transcripts, visuals, observations, and provenance references remain auditable.

Baseline visual events are the scene and periodic candidates produced by deterministic extraction. A successful visual refresh replaces only that source's baseline origin. Dense frames requested by the host are stored with investigation origin and survive baseline refreshes or later visual-stage failures; an existing visual ID cannot silently change origin.

`coverage.json` schema version 2 aggregates the persisted inspection reports into a course-completeness proof, count totals, disclaimers, active source coverage, retired-source tombstones, and warnings. Completeness remains unproven when the expected count is unknown or any expected entry is inaccessible or failed.

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

Contact sheets and dense frames are private, reproducible cache artifacts. Observations contain concise factual conclusions and evidence references, never hidden model reasoning. A generated skill receives only selected visual assets and synthesized claims with provenance.

## Extension points

`SourceAdapter`, `Transcriber`, `OCRProvider`, and `VisionAnalyzer` are narrow abstract interfaces. Source adapters are also exposed through the `video_to_skill.source_adapters` Python entry-point group. An adapter must:

- reject unsupported locators without network access;
- return stable source IDs during inspection;
- materialize only inside the supplied source directory;
- never place secrets in its descriptor or returned paths;
- provide a content hash that changes when reusable evidence changes.

Provider implementations must return typed models and raise domain errors with actionable messages. The pipeline treats unexpected provider exceptions as isolated source/stage failures.

## Generated skill boundary

Before semantic authoring, `blueprint-schema --workspace` emits the strict Pydantic schema and a sanitized seed containing the workspace's exact active, retired, inaccessible, and failed course ledger. The seed contains digests and public metadata rather than local locators, raw failure text, transcripts, frames, or media. `build-skill --workspace` recomputes that ledger and rejects omissions, additions, metadata changes, stale workspace identity, and unsupported coverage upgrades before rendering.

The generated skill is a derivative distribution artifact. It receives selected keyframes and synthesized Markdown, but never the workspace database, raw video/audio, subtitle files, or complete transcript. `sources.md` preserves the verified course accounting, while `provenance.json` connects rendered claims to workspace source IDs, times, modalities, and evidence IDs without copying transcript text. A build without a workspace is explicitly marked unverified and cannot retain a self-asserted coverage ledger.
