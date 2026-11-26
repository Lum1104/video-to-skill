# Named Editions & Evidence Bundles

Advanced workspace workflows for publishing multiple editions from one Analyze pass and exporting reproducible evidence bundles.

## Publish Another Edition

When the user wants a separately named localization or a different already-planned learning path from an existing workspace, create or resume a named edition instead of re-running acquisition and Analyze:

```bash
"$V2S_ENGINE" edition WORKSPACE EDITION-NAME --host codex --output-language LANGUAGE --curriculum PATH-ID --skill-name SKILL-NAME
"$V2S_ENGINE" edition WORKSPACE EDITION-NAME --host claude --output-language LANGUAGE --from-edition SOURCE-EDITION --curriculum PATH-ID --skill-name SKILL-NAME
"$V2S_ENGINE" edition WORKSPACE EDITION-NAME --host codex --output-language LANGUAGE --plan-curriculum --skill-name SKILL-NAME
```

- `--curriculum` binds an existing path without another curriculum-planning worker.
- `--from-edition` pulls the path and logical artifact/claim identity from another named edition (otherwise the legacy single-edition checkpoint is used).
- `--plan-curriculum` runs a new curriculum-planning worker when a genuinely different learning design is requested.
- Resume an edition with:

```bash
"$V2S_ENGINE" edition WORKSPACE EDITION-NAME
```

Key guarantees:
- A named edition never re-acquires, transcribes, extracts visuals, or reruns Analyze. It pins the completed integrated Analyze task, source snapshot, semantic-record digests, and analysis-depth contract, then runs only the needed curriculum checkpoint, Author, behavior trials, independent Review, compilation, validation, and no-clobber installation.
- If source/depth refresh or a new Analyze revision changes that lineage, create a new edition name instead of silently reusing it.
- Submit returned tasks with ordinary `submit WORKSPACE TASK_ID RESULT_FILE`; task scope resolves its edition.
- Preserve semantic IDs and (for localizations of the same curriculum) logical artifact IDs, claim IDs, evidence links, and timestamps. Use `--identity-drift-reason` only for a real structural exception.
- On completion, report the edition ID, configuration digest, pinned lineage, curriculum source/path, any identity-drift justification, and edition-local completion status.

## Export Evidence Bundles

Export evidence only when the user asks for a reproducibility or preservation archive. The engine builds and verifies bundles directly from canonical workspace state without LLM mediation:

```bash
"$V2S_ENGINE" evidence-bundle WORKSPACE --mode compact --output /path/to/course-evidence.v2sbundle
"$V2S_ENGINE" verify-evidence-bundle /path/to/course-evidence.v2sbundle
```

- **Compact bundle** (shareable derivative, not a generated Skill): contains sanitized source metadata, canonical semantic/design/review records, observations and gaps, selected teaching visuals and contact sheets, sanitized tool-run records, and a content-addressed manifest. Excludes raw video/audio, workspace DB, caches, credentials, host paths, and rendered Skills. Transcripts stay excluded unless the user explicitly authorizes redistribution (`--authorize-transcript-redistribution`). Add `--edition EDITION-NAME` to export one edition's downstream lineage.
- **Archival bundle** (private and sensitive): use `--mode archival --confirm-private-archival` only after the user explicitly accepts that boundary. May include raw media, audio, frames, transcripts, canonical intermediates, and a consistent DB snapshot, while still excluding cookies, credentials, auth files, caches, locks, task leases, and rendered Skills. Keep mode `0600` outside the workspace, installed Skill, and Git history. Bundle mode is independent of `--analysis-depth archival`.
