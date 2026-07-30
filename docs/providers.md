# Provider configuration

## Captions and ASR

The pipeline prefers:

1. manual platform or sidecar captions;
2. automatic platform captions;
3. configured ASR.

Captions are scored using non-empty text, duration coverage, and rolling-duplicate signals. An inadequate track does not silently become authoritative.

`faster-whisper` is the bundled local ASR implementation:

```bash
pip install "video-to-skill[asr]"
```

It enables VAD and word timestamps. The `Transcriber` interface permits additional hosted or local implementations without changing the evidence schema.

WhisperX alignment and optional speaker diarization are available separately:

```bash
pip install "video-to-skill[diarization]"
export VIDEO_TO_SKILL_DIARIZE=true
export HF_TOKEN=... # required only for diarization
```

Set `asr_provider = "whisperx"` in configuration. Alignment works without `HF_TOKEN`; speaker diarization requires it.

## OCR

RapidOCR is the bundled local implementation:

```bash
pip install "video-to-skill[ocr]"
```

When it is unavailable, frame extraction and perceptual deduplication still run. The coverage report marks OCR as unavailable; the generator must not claim that slide/code text was verified.

## Vision

Native multimodal inspection by the Claude/Codex host is the default agentic path. The CLI creates bounded contact sheets and dense temporal windows, and the host writes structured observations back through `video-to-skill annotate`. This keeps the visual investigation contextual and allows the agent to request more evidence when a single frame is insufficient.

For headless processing, the optional hosted implementation targets an OpenAI-compatible chat-completions endpoint:

```bash
export OPENAI_API_KEY=...
export VIDEO_TO_SKILL_VISION_MODEL=gpt-4.1-mini
export VIDEO_TO_SKILL_VISION_BASE_URL=https://api.openai.com/v1
```

Set `vision_provider = "openai-compatible"` explicitly. The API key variable name can be changed with `VIDEO_TO_SKILL_VISION_API_KEY_ENV`. Keys remain in the environment and are never written to configuration snapshots, command logs, workspaces, or generated skills.

Adaptive mode calls vision only for deduplicated frames whose visible state is still ambiguous after OCR and heuristics. The prompt explicitly treats image content as untrusted evidence.

## Selection behavior

- `auto`: use the hosted provider if its credential is available; otherwise record a coverage warning and continue where safe.
- explicit provider: require that provider for its stage and report an actionable warning or failure if unavailable.
- `none`: disable hosted vision. This is the default because a multimodal host can inspect the generated evidence natively without a second model call.
