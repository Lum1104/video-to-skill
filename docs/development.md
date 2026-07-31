# Developer guide

This document covers installation, the internal CLI, provider configuration, testing, and evaluation. These commands are implementation and maintenance tools used by the generator Skill; they are not the end-user workflow described in the root README.

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

The public GitHub source is not available yet. Validate the same source-form installation locally without publishing or changing a user-level Skill:

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

After the repository is published, validate the documented remote source from another clean temporary project:

```bash
npx --yes skills add Lum1104/video-to-skill \
  --skill video-to-skill \
  --agent claude-code \
  --agent codex \
  --copy \
  --yes
```

Do not change the README status note or report remote installation as released until this command succeeds against the public repository.

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

The end-user README uses the open `skills` installer because it preserves the standalone `/video-to-skill` and `$video-to-skill` names across Claude Code and Codex. Claude Code namespaces plugin Skills, so a marketplace plugin would change the public invocation contract.

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

### Generate the blueprint authoring contract

Before authoring a course blueprint, derive its schema and full source ledger from the persisted workspace:

```bash
video-to-skill blueprint-schema \
  --workspace ./video_skill_work \
  --output ./course-authoring.json
```

The output is an authoring envelope, not a build input. `blueprint_schema` contains the strict Pydantic JSON Schema, while `blueprint_seed` contains a sanitized starter blueprint with every active, retired, inaccessible, and failed course entry represented. Copy `blueprint_seed` to a separate JSON file, preserve its `sources` and `coverage_ledger` exactly, and fill in the semantic artifacts, claims, principles, and limitations. The envelope never contains transcripts, media, frame data, private local locators, or raw failure reasons.

### Build, validate, and install from a blueprint

`build-skill` is the integrated handoff from agent-authored semantics to a native course Skill:

```bash
video-to-skill build-skill ./course-blueprint.json \
  --host claude \
  --workspace ./video_skill_work \
  --output ./generated-skills/course-name
```

For Codex and project-local discovery:

```bash
video-to-skill build-skill ./course-blueprint.json --host codex --project
```

The input is the completed `blueprint_seed`: a strict `CourseSkillBlueprint` schema-version 1 JSON document. It supplies the Skill identity, scope, prerequisites, core principles, workspace-bound coverage ledger, evidence-linked claims, limitations, optional sanitized assets, and grounded artifacts for Learn, Practice, Apply, and Reference. The schema requires all four modes, at least one exercise and a separate solution or rubric, unique safe paths, and provenance for every rendered artifact.

The command first compares the blueprint ledger with the workspace's current active sources, retired-source tombstones, and complete inspection records. It rejects omitted or invented entries, stale ledgers from another workspace, altered metadata, and any attempt to present incomplete coverage as complete. It then deterministically renders the blueprint into a new portable artifact, runs shareability and supported fenced-code validation, invokes the official `skills-ref` validator when available, and safely installs the result for the selected host. `--workspace` is required for verified full-course accounting and when the blueprint includes visual assets; it also enforces separation between private evidence and shareable output. Without `--output`, the portable artifact defaults to `./generated-skills/<name>`. Without `--project`, installation uses the host's user-level Skill root.

Omitting `--workspace` remains available for standalone schema experiments, but the build record explicitly reports coverage as unverified and discards any unverified ledger supplied by the blueprint.

Rendering refuses an existing output path and never edits the private workspace. Installation is atomic and never overwrites different same-name content. If validation or installation fails after rendering, the portable artifact is retained and its path is reported for repair; it is not presented as installed. On success, text output prints the direct `/name` or `$name` invocation, while `--json` returns the name, generated and installed paths, installation status, host, scope, and validity.

Use the standalone `validate` and `install-generated` commands below for stage-level debugging or maintenance. Agent-driven production generation should use `build-skill` so rendering, validation, preservation on failure, installation, and the completion record stay one transactionally safe workflow.

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

The command runs full validation with supported fenced-code parsing before installation, invokes the official `skills-ref` validator when available, derives the destination name from `SKILL.md` frontmatter, rejects symbolic links, and copies through a temporary staging directory before an atomic rename. Reinstalling byte-identical content is idempotent. Different existing content with the same Skill name is never overwritten; the generated artifact is preserved so the agent can rename it or enter an explicit update workflow.

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

When browser authentication is configured, the engine invokes yt-dlp once to
create a private temporary Netscape cookie jar. Source inspection and every
concurrent materialization then use private per-worker copies through
`--cookies`; they do not reopen the browser cookie database or race while
yt-dlp updates its jar. The temporary directory is user-private on Windows and
additionally uses mode `0700` with `0600` cookie files on POSIX. The jars are
overwritten and removed when the command exits.

As an alternative, point `VIDEO_TO_SKILL_COOKIES_FILE` at an existing Netscape
`cookies.txt` file. Configure only one cookie source. Use a dedicated browser
profile or cookie file with the minimum access required. Never commit or
distribute cookies, workspaces, debug logs, or downloaded media, and never place
cookie text in a generated Skill.

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
sources/
  <source-id>/
    media.*
    captions.*
    audio-16khz.wav
    frames/
    investigation-frames/
    contact-sheets/
```

The SQLite evidence schema is version 4. It stores ordered source descriptors, per-stage state, transcript segments, visual events, semantic sections, agent observations, evidence gaps, input inspection reports, and warnings. Older supported workspaces migrate forward when opened; workspaces created by an unsupported newer schema are refused.

Successful refreshed inspection tombstones sources that disappeared from the latest course inventory instead of deleting them. Active queries omit retired sources by default, while the tombstone retains the descriptor, removal time, and reason. Existing transcripts, frames, observations, and provenance references remain available for audit or an explicit update workflow.

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
