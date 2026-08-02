# Developer guide

This document covers installation, the internal CLI, provider configuration, testing, and evaluation. These commands are implementation and maintenance tools; the root `SKILL.md` owns the host-agent workflow.

## Runtime requirements

- Python 3.11–3.13; Python 3.12 is recommended for broad ML wheel support.
- FFmpeg and ffprobe.
- Network and working TLS certificate access to PyPI for first-use bootstrap and optional capability installation.
- Node.js and `npx` only when validating the Agent Skills installer path.
- Optional local ASR, OCR, alignment, diarization, and hosted vision dependencies. The Python package installs yt-dlp as a base dependency.

On macOS, install the required system media programs with:

```bash
brew install ffmpeg
```

## Set up a development environment

Using `uv`:

```bash
uv sync --extra dev
```

Using a virtual environment and pip:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Add local transcription and OCR when developing those stages:

```bash
pip install -e ".[asr,ocr]"
```

Add WhisperX alignment and optional speaker diarization with:

```bash
pip install -e ".[diarization]"
```

Check the runtime before processing live sources:

```bash
video-to-skill doctor
```

When using a `uv` checkout without an installed executable, replace `video-to-skill` in the examples below with `uv run video-to-skill`.

## Validate installation from a local checkout

Validate the source-form installation from the current checkout without changing a user-level Skill:

```bash
V2S_CHECKOUT="$(pwd)"
V2S_TEST_PROJECT="$(mktemp -d)"
V2S_TEST_RUNTIME="$(mktemp -d)"
(
  cd "$V2S_TEST_PROJECT"
  npx --yes skills add "$V2S_CHECKOUT" \
    --skill video-to-skill \
    --agent claude-code \
    --agent codex \
    --copy \
    --yes
)
VIDEO_TO_SKILL_RUNTIME_ROOT="$V2S_TEST_RUNTIME" \
  "$V2S_TEST_PROJECT/.agents/skills/video-to-skill/scripts/video-to-skill" --version
```

This copies the standalone Skill into the temporary project's `.claude/skills` and `.agents/skills` roots, then exercises its actual first-use launcher. The first run needs PyPI network access and can take longer while it prepares the isolated runtime. Run the final launcher command a second time to verify reuse. The `V2S_TEST_PROJECT` and `V2S_TEST_RUNTIME` variables identify the temporary artifacts for inspection or later cleanup.

Validate the published repository from another clean temporary project:

```bash
npx --yes skills add Lum1104/video-to-skill \
  --skill video-to-skill \
  --agent claude-code \
  --agent codex \
  --copy \
  --yes
```

Treat the local and remote checks as separate surfaces: the local command validates the working tree, while the remote command validates what GitHub currently serves.

## Source-form private runtime

The repository root is an installable standalone Skill because it contains `SKILL.md`, `pyproject.toml`, `src/`, and the cross-platform launchers under `scripts/`. A standard Agent Skills installer copies the complete source tree rather than only the instructions.

On first invocation, the source launcher builds a fingerprint from the Python major/minor version, resolved source root, `pyproject.toml`, bootstrap engine, POSIX and Windows launchers, and `.py` or `py.typed` files under `src/`. Under a bounded inter-process lock, it creates `video-to-skill/runtimes/<fingerprint>/` in the platform user-data directory, installs the local source plus base third-party dependencies, verifies the engine with a bounded import probe, and writes `runtime.json`. Matching healthy runtimes are reused. A changed fingerprint creates a new runtime; a damaged runtime is removed and rebuilt at most once for the invocation.

The source tree remains the installed engine, while its third-party dependencies are fetched from PyPI. This is why first-use setup requires network access even though `video-to-skill` itself comes from the local or GitHub checkout.

After inspection selects an evidence route, the agent can enable a missing private-runtime capability without exposing package management to the user:

```bash
scripts/video-to-skill ensure-capability asr
scripts/video-to-skill ensure-capability ocr
scripts/video-to-skill ensure-capability diarization
```

Each command installs the matching editable extra from the same source tree, validates it with a bounded import probe, and becomes an idempotent no-op when healthy. ASR is appropriate when captions are unavailable or inadequate, OCR when local text extraction is material, and diarization only when speaker separation is explicitly required. Extra wheels and later model downloads can be large, so do not install all capabilities eagerly.

The open `skills` installer preserves the standalone `/video-to-skill` and `$video-to-skill` names across Claude Code and Codex. Claude Code namespaces plugin Skills, so a marketplace plugin would change the public invocation contract.

## Install a compact package bundle

The Python package also ships the generator instructions and portable engine entry as wheel resources. Maintainers can install an invocation-complete bundle into an Agent Skills root with:

```bash
video-to-skill install-skill ~/.claude/skills
video-to-skill install-skill ~/.agents/skills
```

The installed bundle has this shape:

```text
video-to-skill/
├── SKILL.md
├── runtime.json
└── scripts/
    ├── engine.py
    ├── video-to-skill
    └── video-to-skill.cmd
```

The POSIX and Windows launchers are generated during installation and bind to the exact Python or virtual-environment executable running the installer. `engine.py` supports both an installed package and a source checkout. `runtime.json` records the engine version, entry asset, interpreter, and launcher paths for diagnostics. The generator Skill resolves this bundle relative to its own `SKILL.md`; it does not ask the end user to activate an environment, find an executable on `PATH`, or run a pipeline command.

The first install stages the complete directory before placing it in the selected Skill root. Repeating an identical install is idempotent. Different instructions, missing or modified runtime assets, non-executable POSIX launchers, symbolic links, and other conflicting bundle state are not overwritten by default. `--force` updates the managed files while retaining unrelated regular files, but still refuses a bundle tree containing symbolic links.

Claude Code discovers user Skills under `~/.claude/skills` and project Skills under `.claude/skills`. Codex discovers user Skills under `~/.agents/skills` and project Skills under `.agents/skills`.

## Internal pipeline workflow

The generator orchestrates these commands. They are also useful when developing, debugging, or evaluating an individual pipeline stage.

### Inspect inputs

Inspection resolves course structure and estimates work without downloading media:

```bash
video-to-skill inspect "https://www.youtube.com/playlist?list=..."
video-to-skill inspect "https://www.bilibili.com/video/BV..."
video-to-skill inspect ./course-recording.mp4
```

For agent orchestration, `--json` returns a stable top-level object rather than a bare source list:

```bash
video-to-skill inspect "https://www.youtube.com/playlist?list=..." --json
```

The object contains `sources`, the accessible normalized source descriptors, and `completeness`, one audit record per input. Each completeness record includes the expected, accessible, inaccessible, and failed entry counts; whether completeness was proven; any disclaimer; and an ordered `entries` list. Entry status is `accessible`, `inaccessible`, or `failed`; inaccessible and failed entries retain a reason even when no source descriptor can be created. `expected_entries` is `null` when the adapter cannot prove the input's total size, and `completeness_proven` is true only when every expected entry is accessible with no inspection failures.

### Extract or resume evidence

Extraction creates a normalized, queryable evidence workspace. Reusing the same path resumes completed stages whose cache keys still match:

```bash
video-to-skill extract \
  "https://www.youtube.com/playlist?list=..." \
  --workspace ./video_skill_work
```

The extractor records isolated source failures without discarding successful course items.

The engine records deterministic subprocess and provider executions directly in SQLite. Export the canonical sanitized view only when debugging or reproducing a workspace:

```bash
video-to-skill tool-runs ./video_skill_work
```

The command creates `logs/tool-runs.jsonl` inside the private workspace and reports its SHA-256 digest. It accepts an existing export only when the bytes are identical and never overwrites a different file. Records have stable logical IDs, immutable generation-numbered attempt histories, cache-hit counts, status and duration, tool versions when resolvable, normalized non-secret arguments, input SHA-256 values, workspace-relative file digests, and verified semantic digests for typed ASR, OCR, and vision records. They never contain raw stdout, stderr, environment variables, cookies, authorization values, expiring URLs, or private absolute paths, and they are not copied into generated Skills.

### Export or verify evidence bundles

Create a compact reproducibility bundle outside the workspace:

```bash
video-to-skill evidence-bundle ./video_skill_work --mode compact --output ./video_skill_work-evidence.v2sbundle
video-to-skill verify-evidence-bundle ./video_skill_work-evidence.v2sbundle
```

Compact export is a code-owned allowlist and is independent of generated Skill compilation and `analysis_depth`. It includes sanitized source metadata, available canonical Analyze/design/review records, observations, gaps, selected visual derivatives and contact sheets, build reports, and the deterministic sanitized tool-run JSONL. It omits raw video/audio, SQLite, caches, task data, rendered Skill previews, credentials, signed URLs, and private absolute paths. Transcript and caption content is omitted by default; add `--authorize-transcript-redistribution` only after confirming that redistribution is permitted. Use `--edition EDITION-NAME` to bind the export to that edition's immutable downstream lineage.

Private archival export requires a deliberate acknowledgement:

```bash
video-to-skill evidence-bundle ./video_skill_work --mode archival --confirm-private-archival --output ./video_skill_work-private.v2sbundle
```

The archival bundle may include raw media/audio, all retained frames, normalized transcript evidence, explicitly declared external local caption sidecars, canonical intermediates, and a synthesized evidence-only SQLite database for future reanalysis. It still excludes cookies, authentication and credential files, caches, locks, temporary downloads, task leases, rendered behavior targets, and generated Skill files; sanitized workspace and edition metadata never exposes output, project, install, or host paths. The file is created with mode `0600`, must stay outside the workspace and normal Git/Skill distribution, and is never overwritten. Stable ZIP metadata, sorted members, and content digests make unchanged exports byte-identical, while atomic create-only publication makes concurrent writers safe. Verification rejects symlinks, special members, duplicates, traversal names, unexpected members, identity drift, size mismatches, and checksum failures without extracting the bundle.

### Query bounded evidence

List the course inventory and semantic sections:

```bash
video-to-skill query ./video_skill_work --inventory
```

Retrieve one section or run a precise FTS5 search:

```bash
video-to-skill query ./video_skill_work --source 1 --section 2
video-to-skill query ./video_skill_work --source 1 --search "deployment AND rollback"
```

Use bounded queries rather than loading complete transcripts or opening the SQLite database directly.

### Run the multimodal investigation loop

The first extraction is a visual index, not the final interpretation. Generate a bounded overview for native host inspection:

```bash
video-to-skill contact-sheet ./video_skill_work --source 1 --section 2
```

Retrieve the synchronized transcript, visual events, stored observations, and evidence identifiers:

```bash
video-to-skill context ./video_skill_work --source 1 --section 2 --format json
video-to-skill context ./video_skill_work --source 1 --at 127 --window 8 --format json
```

If a contact sheet cannot establish an action or state transition, extract a short dense window:

```bash
video-to-skill frames ./video_skill_work --source 1 --from 120 --to 135 --fps 2
```

Persist structured, source-grounded observations:

```bash
video-to-skill annotate ./video_skill_work ./observations.json
```

Recompute unresolved evidence gaps:

```bash
video-to-skill gaps ./video_skill_work --source 1 --format json
```

The host repeats synchronized context, visual inspection, dense sampling, annotation, and gap analysis only where important claims remain under-supported. See the root [`SKILL.md`](../SKILL.md) for observation schemas, investigation budgets, stopping rules, and generation policy.

### Run the workspace-centered generation workflow

Start a durable conversion with the artifact language and analysis-depth intent made explicit:

```bash
video-to-skill run \
  "https://www.youtube.com/playlist?list=..." \
  --workspace ./video_skill_work \
  --host codex \
  --output-language English \
  --analysis-depth auto
```

`run` advances deterministic work until it returns either `actions-required` or `complete`. Each action points at a bounded task directory whose packet, schema, lease, and output boundary are already materialized. A worker reads those files, writes `TASK_PATH/output/result.json`, and submits the result directly:

```bash
video-to-skill submit ./video_skill_work TASK_ID TASK_PATH/output/result.json
video-to-skill run --workspace ./video_skill_work
```

Repeat the resume command after the returned parallel action group completes. Do not retransmit sources, host, output, installation scope, language, or depth settings on resume. The coordinator validates every submission, persists immutable canonical revisions, runs the curriculum checkpoint before full artifact Authoring, renders an immutable behavior target, dispatches isolated behavior trials and an independent Review, and then compiles the internal schema-version 2 `CourseSkillBlueprint` from canonical workspace state. `blueprint-schema` and `build-skill` are not public commands.

The generated Skill always has the fixed portable records documented in [Generated Skill V2](generated-skill-v2.md) plus at least one evidence-grounded authored artifact. Learn, Practice, Apply, and Reference are capability levels rather than required directories or quotas. Rendering, final validation, no-clobber installation, and completion persistence remain one coordinator-owned workflow.

### Publish another edition without reanalysis

Create a named edition when the user wants a localization or a different learning path over the completed Analyze map:

```bash
video-to-skill edition ./video_skill_work chinese-course \
  --host codex \
  --output-language zh-CN \
  --curriculum PATH-ID \
  --skill-name chinese-course
```

Use `--plan-curriculum` instead of `--curriculum` only when a new learning design is required. Use `--from-edition SOURCE-EDITION --curriculum PATH-ID` when reusing another edition's checkpoint and logical identity baseline. Resume without restating immutable configuration:

```bash
video-to-skill edition ./video_skill_work chinese-course
```

An edition reuses the pinned integrated Analyze task and creates only edition-local curriculum, Author, behavior, Review, build, and completion state. It never accepts sources or `--refresh`; changed Analyze lineage requires a new edition name.

### Validate a generated Skill

Validate format, provenance, links, secrets, shareability, and supported fenced code without executing source-derived code:

```bash
video-to-skill validate /path/to/generated-skill --check-code
```

The validator uses the official `skills-ref` validator when it is installed unless `--skip-official` is selected.

### Install a generated course Skill

The generator uses `install-generated` after synthesis and validation. Maintainers can exercise the same host-aware path directly:

```bash
video-to-skill install-generated /path/to/generated-skill --host claude
video-to-skill install-generated /path/to/generated-skill --host codex
```

User-scope installation targets `~/.claude/skills` for Claude Code and `~/.agents/skills` for Codex. Add `--project` to target `.claude/skills` or `.agents/skills` under the current project instead:
```bash
video-to-skill install-generated /path/to/generated-skill --host claude --project
video-to-skill install-generated /path/to/generated-skill --host codex --project
```

The command runs full validation with supported fenced-code parsing before installation, invokes the official `skills-ref` validator when available, derives the destination name from `SKILL.md` frontmatter, rejects symbolic links, and copies through a temporary staging directory before an atomic rename. Reinstalling byte-identical content is idempotent. Different existing content with the same Skill name is never overwritten; preserve the generated artifact and choose a different name or destination because update and fold-in are not implemented.

### Reclaim workspace storage

Remove reproducible cached media and frames while preserving manifests, captions, the evidence database, and other retained evidence:

```bash
video-to-skill clean ./video_skill_work
```

The command asks for confirmation. Use `--yes` only in controlled automation.

## Configuration

Configuration is loaded in this order, with later sources taking precedence:

1. The platform-specific user configuration file.
2. `./video-to-skill.toml`.
3. The file supplied with `--config`.
4. `VIDEO_TO_SKILL_*` environment variables.
5. CLI overrides.

See [`video-to-skill.toml.example`](../video-to-skill.toml.example) for operational settings. API keys are environment-only and are not serialized into manifests.

Use one product-level setting for evidence recall and retention:

```toml
[video_to_skill]
analysis_depth = "auto" # auto | standard | deep | archival
```

The equivalent environment variable is `VIDEO_TO_SKILL_ANALYSIS_DEPTH`, and `inspect`, `extract`, and `run` accept `--analysis-depth`. `auto` is the default and deterministically selects `standard` or `deep` from inspectable source/course density. `archival` is explicit opt-in. Inspect JSON includes the recommendation, reasons, and effective non-secret budgets before processing begins.

Existing frame and segment settings remain advanced baseline overrides; the selected depth scales them and then applies hard safety maxima. Depth does not select providers or credentials. `visual_profile = "transcript"` still disables visual processing, and `vision_provider = "none"` remains unchanged at every depth.

The requested/effective contract is persisted in `manifest.json` and `analysis/run-config.json`. Resume must match the persisted request and profile version. Use `--refresh` when source inventory or inspectable density has changed; refresh recomputes the contract and affected cache keys. Legacy workspaces are assigned a marked compatibility contract before any new Analyze snapshot.

Named editions do not refresh evidence. Immutable edition state lives at `editions/<edition-id>/edition.json`; edition-local orchestration uses `editions/<edition-id>/analysis/`, and build receipts use `editions/<edition-id>/builds/`. The state pins the integrated Analyze task, workspace/source snapshot, canonical Analyze digests, and depth-contract digest. `submit` resolves the edition from task scope rather than ambient process state. If any pinned value differs, resume fails and a new edition must be created after refreshed Analyze work.

Existing unnamespaced `analysis/run-config.json`, language state, tasks, canonical heads, and `builds/` remain the legacy compatibility edition. No database rewrite is required: post-Analyze record IDs are internally prefixed with the deterministic edition ID, while Analyze record IDs remain shared. Cross-run dependencies let an edition's first downstream task depend on the completed integrated Analyze task; acquisition and Analyze tasks are never materialized in the edition run.

The default safety limits include 500 course items, eight hours per source, 20 GiB per local file or download, two concurrent source workers, and a two-hour subprocess timeout. The remote source adapter selects analysis media at up to 720p when media is required.

### Platform authentication

Platform authentication is opt-in:

```bash
export VIDEO_TO_SKILL_COOKIES_FROM_BROWSER=chrome
```

When browser authentication is configured, the engine invokes yt-dlp once to create a private temporary Netscape cookie jar. Source inspection and every concurrent materialization then use private per-worker copies through `--cookies`; they do not reopen the browser cookie database or race while yt-dlp updates its jar. The temporary directory is user-private on Windows and additionally uses mode `0700` with `0600` cookie files on POSIX. The jars are overwritten and removed when the command exits.

As an alternative, point `VIDEO_TO_SKILL_COOKIES_FILE` at an existing Netscape `cookies.txt` file. Configure only one cookie source. Use a dedicated browser profile or cookie file with the minimum access required. Never commit or distribute cookies, workspaces, debug logs, or downloaded media, and never place cookie text in a generated Skill.

### Native and hosted vision

Native Claude Code or Codex multimodal inspection is the preferred interactive path. The hosted `vision_provider` defaults to `none`, so extraction does not make an additional frame-by-frame vision API call.

For explicitly authorized headless processing, set `vision_provider = "openai-compatible"` and supply the configured API key environment variable:

```bash
export OPENAI_API_KEY=...
export VIDEO_TO_SKILL_VISION_MODEL=...
export VIDEO_TO_SKILL_VISION_BASE_URL=https://api.openai.com/v1
```

See [Provider configuration](providers.md) for ASR, OCR, diarization, and vision details.

## Evidence workspace

The private, resumable workspace has this shape:

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

The SQLite evidence schema is version 10. It stores ordered source descriptors, per-stage state, transcript segments, visual events, semantic sections, agent observations, evidence gaps, input inspection reports, orchestration state, immutable publication heads, identity baselines, sanitized logical tool runs with immutable execution attempts, and warnings. WAL mode and one connection per operation allow concurrent source workers and tool-run writers to commit safely. Older supported workspaces migrate forward when opened; workspaces created by an unsupported newer schema are refused.

Successful refreshed inspection tombstones sources that disappeared from the latest course inventory instead of deleting them. Active queries omit retired sources by default, while the tombstone retains the descriptor, removal time, and reason. Existing transcripts, frames, observations, and provenance references remain available for audit or a future independently designed update workflow.

Visual events carry an origin of `baseline` or `investigation`. A normal extraction refresh replaces only baseline scene and periodic evidence. Dense windows added by the host investigation loop retain the investigation origin and survive later baseline refreshes or visual-provider failures. A visual evidence ID cannot silently change origin.

`coverage.json` uses schema version 2. Its `course_completeness` object aggregates expected, accessible, inaccessible, and failed entry counts; the proof flag; disclaimers; and the complete per-input inspection records. It also lists active source coverage, retired source tombstones, and warnings. An unknown expected count or any inaccessible or failed entry prevents the report from claiming proven course completeness.

Media and extracted frames are reproducible private cache artifacts. See [Architecture](architecture.md) for lifecycle, concurrency, schema boundaries, and extension interfaces.

## Development checks

Run the same static and test checks used by the project:

```bash
ruff format --check .
ruff check .
mypy
pytest --cov=video_to_skill
agentskills validate "$(pwd)"
```

With `uv`:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest --cov=video_to_skill
uv run agentskills validate "$(pwd)"
```

The normal suite uses recorded source metadata and synthetic media, never mutable live platform responses, private cookies, paid model APIs, or copyrighted media. Run live YouTube or Bilibili smoke tests separately with explicit credentials.

## Evaluation

For a labeled evaluation corpus, score critical visual-state recall and semantic boundaries with:

```bash
video-to-skill evaluate ./video_skill_work ./evaluation-labels.json
```

The command fails when labeled critical visual recall is below 90% by default. Use `--required-visual-recall` to set a different explicit threshold for an experiment.

## Extension points

`SourceAdapter`, `Transcriber`, `OCRProvider`, and `VisionAnalyzer` are narrow interfaces. Source adapters and providers can be registered through the Python entry-point groups declared in `pyproject.toml`.

Provider implementations must return typed models, keep materialized files within the supplied workspace, and raise actionable domain errors that do not expose credentials or raw response headers. Add unit or synthetic-media coverage for every behavior change.

See [Security](security.md) before changing source acquisition, subprocess handling, authentication, hosted providers, annotation ingestion, validation, or generated artifact policy.
