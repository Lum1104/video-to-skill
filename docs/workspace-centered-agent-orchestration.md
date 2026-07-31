# Workspace-Centered Agent Orchestration

## Status

This document records the implemented workspace-centered generation architecture.

The product-quality motivation is documented in [ambitious-startups-v1-v2-product-audit.md](ambitious-startups-v1-v2-product-audit.md). The broader generated-Skill contract is documented in [generated-skill-v2.md](generated-skill-v2.md).

## Confirmed architecture

The `video-to-skill` engine does not call an LLM.

Semantic interpretation, multimodal judgment, curriculum design, artifact authoring, and independent criticism run through host-native agents. The engine remains provider-neutral and owns no model credentials, model selection, model billing, or separate text-model runtime.

The durable workspace is the data plane. It owns task packets, lifecycle state, evidence boundaries, semantic results, artifact drafts, reviews, reports, and canonical revisions.

> An agent is a reasoning worker, not a data transport layer.

The main agent exchanges only workspace paths, task IDs, expert personas, typed user decisions, completion states, material blockers, and the final summary. It does not copy transcripts, frames, evidence IDs, semantic maps, drafts, critic reports, or blueprints between tools and workers.

## Public protocol

The normal orchestration surface contains two commands:

```bash
video-to-skill run SOURCES... --workspace WORKSPACE --host codex
video-to-skill run --workspace WORKSPACE
video-to-skill submit WORKSPACE TASK_ID RESULT_FILE
```

The first `run` creates or resumes evidence extraction, records the host and output configuration, and advances every deterministic transition until agent work or a material user decision is required.

The resume form reads sources, host, output, installation scope, and validation settings from the workspace. The main agent does not need to retransmit them.

`submit` validates and atomically persists one role-specific result. Workers call it themselves after writing `TASK_PATH/output/result.json`.

The former public `blueprint-schema` and `build-skill` authoring commands are removed. The strict blueprint remains an internal compilation boundary and build receipt.

Bounded `query`, `context`, `contact-sheet`, `frames`, `annotate`, and `gaps` commands remain available to workers and developers, but they are not the main-agent workflow.

## Run outcomes

`run` emits one JSON envelope and writes progress to stderr.

### `actions-required`

A `dispatch-agent` action contains:

- a stable task ID;
- an absolute task directory;
- the Analyze, Author, or Review role;
- a domain-specific persona hint;
- a parallel group; and
- whether the task already has a durable lease.

An `ask-user` action contains:

- a stable decision task ID;
- the material decision prompt; and
- bounded options produced by canonical Author state.

The coordinator materializes and leases every action before returning it. A resumed main agent can redispatch an already-leased task from its durable directory; duplicate completion is detected by result digest.

### `complete`

A complete envelope records:

- build ID and generated Skill name;
- portable and installed paths;
- installation status and host;
- workspace path and retention;
- source counts and failures;
- source, semantic, and instructional-affordance coverage;
- critic repair count;
- validation-report path; and
- invocation form.

Only the engine's complete envelope proves that canonical state compiled, validated, and installed.

Irrecoverable failures use ordinary command failure semantics rather than another workflow state.

## Workspace ownership

SQLite owns identity, dependencies, execution state, leases, attempts, immutable result metadata, canonical revisions, and canonical heads.

Workspace files own task packets, JSON schemas, task results, semantic snapshots, curriculum records, artifact plans, Markdown drafts, critic reports, behavior reports, validation reports, and build receipts.

Agents never write SQLite directly.

The main durable database records are:

```text
analysis_runs
work_items
work_item_dependencies
work_results
canonical_records
canonical_heads
```

This deliberately replaces the earlier proposal for many phase-specific tables. Semantic and design payloads are immutable files indexed by generic canonical records; execution state remains small.

Each canonical revision records:

- kind and logical record ID;
- monotonically increasing revision;
- workspace-relative immutable path;
- SHA-256 digest;
- producer task ID;
- source snapshot digest; and
- creation time.

The canonical head points to the accepted revision. A valid submission advances the head atomically after file, schema, evidence, snapshot, and lease validation.

## Task contract

Each task is content-derived from its run, role, scope, dependencies, and source snapshot.

```text
analysis/tasks/<task-id>/
├── task.json
├── packet.json
├── result-schema.json
├── lease.json
└── output/
```

`task.json` provides stable routing metadata. `packet.json` owns bounded evidence or canonical-record references. `result-schema.json` owns the strict worker contract. `lease.json` contains the opaque token and expiry. `output/` is the only writable result boundary.

The main agent dispatches a worker with only the expert persona, task path, engine launcher, workspace, and instruction to submit directly.

Result files and canonical outputs must be regular non-symlink files under the task output directory. Submission rejects path traversal, stale leases, changed source snapshots, wrong roles, wrong task IDs, invalid schemas, forged evidence IDs, source/time-bound violations, digest drift, and incompatible duplicate completion.

## Minimal lifecycle

The work-item lifecycle is:

```text
pending
→ leased
→ complete
  or failed
```

An expired lease returns to `pending`. An invalid submission returns an actionable error without creating submitted or rejected states.

A failed quality review is still a completed Review execution. It creates a new dependent Author task instead of changing the original task to a follow-up state.

Candidate versus canonical status is represented by immutable file revisions and canonical heads, not by generic task states.

## Logical workflow

The semantic workflow is:

```text
Analyze → Author → Review
```

These are reasoning boundaries rather than public commands.

### Analyze

Analyze combines high-recall semantic extraction, terminology normalization, relation proposals, materiality and disposition review, conflict capture, semantic coverage, and evidence-bounded capability ceilings.

Short interviews and compact courses use one integrated Analyze task. Long courses and playlists use bounded section-group tasks followed by a dependent integration task of the same role.

Talking-head sections are speech-first. Slides, code, UI, diagrams, and physical procedures use multimodal packets only where visual evidence is material.

Frame discovery remains deterministic engine work. Baseline extraction retains the first frame, frames above the configurable scene-change threshold, and periodic fallback frames, then removes near-duplicates with perceptual hashes and adds OCR and visual-type hints. If a semantic action or state is ambiguous, an Analyze worker may request a dense window, but the engine enforces 0.1–30 FPS, at most 300 seconds, at most 1,800 sampled frames, bounded dimensions, workspace-only paths, and perceptual deduplication.

Analyze decides teaching value rather than pixel output. It may propose at most 24 non-duplicate candidates, each grounded in one source and one or more semantic units, using one complete frame, one normalized crop, or an ordered sequence of two to four frames. A candidate must explain why text is insufficient; Analyze cannot provide an arbitrary image path or mutate pixels.

Analyze submissions validate source scope, timestamps, packet evidence IDs, semantic relation endpoints, merge chains, capability evidence, coverage totals, and candidate-to-visual grounding before becoming canonical. The engine then decodes the referenced evidence, applies the normalized crop, composes ordered sequences, strips metadata, writes deterministic PNGs, records dimensions and SHA-256 digests, and publishes immutable candidate records. This adds no engine-side LLM call.

### Author

Author consumes canonical semantic files by path and digest, not copied JSON in a main-agent prompt.

It produces:

- course identity and interaction behavior;
- capability profile within Analyze ceilings;
- curriculum paths and any material user choice;
- artifact specifications;
- task-owned Markdown drafts;
- claims and provenance;
- selections from verified visual candidates and limitations; and
- a complete instructional-affordance ledger.

The instructional ledger is separate from semantic coverage. It records learning objectives, misconceptions, retrieval and transfer prompts, focused exercises, success criteria, scoring, progressive hints, retry, capstone synthesis, operational playbooks, expected states, validation, recovery, quick reference, and decision rules.

Every ledger entry is `provided`, `unsupported`, or `not-applicable` with a rationale. Strong capability claims require the complete relevant surface; weaker levels require proportionally smaller surfaces. This is a capability-consistency rule, not an artifact quota.

Artifact bodies remain Markdown files. Artifact metadata stores a task-output-relative draft path and SHA-256 digest. Submission copies accepted drafts into immutable canonical revisions.

Author receives candidate metadata and verified workspace-relative PNG paths. It selects only visuals that a specific artifact needs, assigns a portable `assets/*.png` destination, links each selected image from every `used_by` Markdown artifact, and binds it to claims that retain the candidate's visual or temporal evidence. Submission rejects unknown candidates, invented derivations, missing Markdown links, unrelated semantic units, and claims that dropped the underlying frame IDs.

### Review

Review must use a producer identity independent of the Author producer.

The review snapshot includes semantic records, curriculum, interaction, capability profile, artifact plan, instructional-affordance ledger, claims, assets, every canonical draft digest, the visual-candidate manifest, and selected image digests.

Review audits semantic retention and instructional-affordance retention separately, followed by grounding, disclosure, runtime behavior, safety, scope, and shareability. For every selected visual, Review checks necessity, legibility, retained context, temporal ordering, privacy, claim grounding, and whether the generated Skill opens it only on demand.

A pass requires no blocking findings and no failed behavior checks. A fail requires a blocking finding or failed behavior check.

A failed Review creates a complete Author revision task and a new independent Review. The coordinator permits at most three repair cycles.

## Deterministic compilation

Compilation begins only after canonical critic and behavior reports pass.

The compiler:

1. verifies every canonical record digest;
2. loads the workspace-derived source and coverage ledger;
3. validates semantic, curriculum, affordance, claim, asset, candidate-image, and artifact records and their digests;
4. resolves artifact drafts by canonical path and digest;
5. constructs the strict `CourseSkillBlueprint` in memory;
6. revalidates source inventory and coverage against the workspace;
7. writes a content-free workspace blueprint receipt;
8. renders the portable Skill outside the private workspace;
9. runs structural, grounding, shareability, and code-fence validation; and
10. installs through the no-clobber installer.

The workspace receipt stores artifact paths and digests, not embedded Markdown bodies. It is written under:

```text
builds/<build-id>/
├── blueprint.json
├── critic-report.json
├── behavior-report.json
├── validation-report.json
└── completion.json
```

If a process stops after rendering but before completion persistence, resume accepts an existing portable output only when its build manifest has the same build ID.

## Concurrency, recovery, and safety

SQLite WAL mode supports concurrent submissions. Lease validation and source-snapshot checks prevent incompatible workers from silently completing the same task.

Canonical publication uses short transactions. Files are copied to immutable revision paths before canonical heads advance; failed transactions leave no accepted state.

Task results persist across conversation loss. A new main agent can call `run --workspace WORKSPACE`, redispatch returned actions, and continue without reconstructing semantic state.

Secrets, cookies, authorization headers, expiring URLs, private chain-of-thought, and private paths intended for shareable output never enter task packets, reports, or the generated Skill.

## Deliberate exclusions

The first implementation supports new Skill generation only.

The following are not compatibility targets in this implementation:

- the former host-authored blueprint workflow;
- update or fold-in of an existing generated Skill;
- prior-Skill artifact-plan merging;
- a parallel MCP orchestration surface; and
- direct engine access to model providers.

These capabilities may be designed independently after the new-generation workflow is stable.

## Acceptance criteria

The implementation is successful when:

- the normal main-agent protocol uses `run` and workers use `submit`;
- the main agent never receives a complete transcript, semantic map, artifact body, or critic report;
- workers operate from task paths and persist results directly;
- short content follows Analyze, Author, Review, compile, validate, and install;
- long content fans out without unbounded packets;
- stale, forged, duplicate, and out-of-scope submissions are rejected;
- semantic and instructional-affordance coverage survive conversation loss;
- Review is independent and repairs are immutable;
- the engine remains model-agnostic; and
- the final blueprint is compiled from canonical workspace state.
