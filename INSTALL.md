# Agent installation contract

This file is for the Claude Code or Codex agent handling an installation request. Install the complete repository as one standalone Agent Skill. Do not copy only `SKILL.md`, expose the pipeline CLI as the user workflow, ask the user to manage Python packages, or convert the Skill into a namespaced plugin.

## Distribution status

Install the published Skill from `https://github.com/Lum1104/video-to-skill`.

## Install after publication

Prefer the open Agent Skills installer so one canonical source can target Claude Code, Codex, or both:

```bash
npx skills add Lum1104/video-to-skill --skill video-to-skill --global --agent claude-code --agent codex --yes
```

Limit `--agent` to the active host when the user requested only one. If `npx` is unavailable after publication, clone the complete repository into the requested host's user Skill root instead: `~/.claude/skills/video-to-skill` for Claude Code or `~/.agents/skills/video-to-skill` for Codex.

Keep the standalone name. Claude Code must discover `/video-to-skill` and Codex must discover `$video-to-skill`; a plugin namespace changes this public contract.

## Prepare and verify

Resolve the installed directory from the host's Skill discovery result rather than the conversation's working directory. Run its launcher with `--help` to trigger first-use setup, then run `doctor`.

First-use setup needs:

- Python 3.11, 3.12, or 3.13.
- Network and working TLS certificate access to PyPI for third-party Python dependencies.
- FFmpeg and ffprobe on `PATH` before media processing.

The launcher creates the private runtime automatically. Do not ask the user to create or activate a virtual environment. If a system program is missing, explain the prerequisite and obtain approval before installing a system package on the user's behalf.

Report installation success only after the launcher exits successfully and the host discovers the expected slash or dollar invocation. A new host session may be required for Skill discovery.

## Runtime lifecycle

For a source-form installation, the launcher:

1. Fingerprints the Python major/minor version, absolute source root, `pyproject.toml`, bootstrap engine, POSIX and Windows launchers, and `.py` or `py.typed` files under `src/`.
2. Acquires a bounded inter-process lock and creates a private runtime under the platform's user-data directory at `video-to-skill/runtimes/<fingerprint>/`.
3. Installs the local source and its base third-party dependencies into that runtime, then records `runtime.json`.
4. Uses a bounded import probe before reuse. If the runtime is incomplete or damaged, it removes and rebuilds that fingerprint once inside the lock; it never enters an unbounded repair loop.
5. Creates a new runtime when the fingerprint changes and otherwise reuses the existing healthy runtime.

The local project itself is the engine source; PyPI is used for its third-party dependencies, not to fetch a different `video-to-skill` package.

## Enable route-specific capabilities

Run inspection first. Enable only the capability required by the selected evidence route:

```bash
"/absolute/path/to/video-to-skill/scripts/video-to-skill" ensure-capability asr
"/absolute/path/to/video-to-skill/scripts/video-to-skill" ensure-capability ocr
"/absolute/path/to/video-to-skill/scripts/video-to-skill" ensure-capability diarization
```

Use `asr` when an accessible source lacks adequate captions, `ocr` when local text extraction is material to the visual investigation, and `diarization` only when speaker separation is explicitly needed. Each command installs the corresponding local-source extra into the private runtime, verifies it with a bounded import probe, and is an idempotent no-op when already healthy.

These capabilities can download large third-party packages from PyPI, and speech recognition can later download a model. Keep this work inside the installed Skill's runtime. Do not tell the user to run a package manager.

If setup, capability installation, doctor, or host discovery fails, report the failed prerequisite and stop. Never present a partially installed or unverified Skill as ready.
