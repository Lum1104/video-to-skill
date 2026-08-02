# Security and distribution model

## Untrusted input

URLs, captions, video descriptions, OCR, contact sheets, dense frames, and visible text are untrusted. They may contain prompt injection, shell commands, secrets, malicious filenames, or misleading instructions.

The implementation:

- invokes subprocesses with argument arrays and no shell;
- accepts remote inputs only from YouTube and Bilibili adapters;
- resolves source cache directories beneath the workspace and rejects source IDs that escape that boundary;
- writes default dense-window frames only to `sources/<source-id>/investigation-frames`, rejects symbolic-link components in that path, verifies containment before extraction, and rechecks it before retaining each frame;
- applies duration, local-file-size, subprocess-timeout, and worker limits;
- redacts credential-like values from subprocess errors;
- records tool executions through a recursive sanitizer that removes secret values, URL queries, external absolute paths, environment data, and raw stdout or stderr;
- tells the generator agent never to follow source instructions;
- validates agent annotations against same-source transcript and frame IDs before persisting them;
- bounds decoded frame dimensions and pixel counts, contact-sheet source and output pixels, dense-frame windows, frame rate, context size, and annotation payloads;
- never executes extracted commands by default.

Baseline extracted frames are capped at 3840×2160 and 4,194,304 pixels; dense investigation frames are further capped at 1920 pixels wide while retaining the same height and pixel ceilings. Contact-sheet inputs are rejected above 25,000,000 pixels and rendered sheets above 32,000,000 pixels. These independent axis and total-pixel checks protect extreme portrait and landscape inputs as well as conventionally sized video.

FFmpeg and yt-dlp process adversarial external content. Keep them patched and run untrusted large-scale jobs in an OS/container boundary with filesystem, CPU, memory, network, and disk quotas.

## Credentials

Platform cookies and provider keys are opt-in. Prefer environment variables and a dedicated browser profile. Browser cookies are decrypted once per engine invocation into an ephemeral jar; concurrent download workers receive isolated copies so yt-dlp cannot race while updating the file. Those jars are never part of the evidence workspace or generated Skill and are removed when the authentication session closes. A user-provided cookie file is snapshotted for the run and never modified in place. Sanitized tool records may retain the name of a credential-bearing option but always replace its value; the exporter also strips URL queries and external absolute paths. Do not pass cookies, workspaces, or debug logs to third parties before reviewing them.

Native host vision avoids sending frames to an additional provider, but it does not make the content trusted. The agent records concise observations and evidence links rather than private reasoning, copied instructions, or unrestricted OCR dumps.

## Shareable output

Validation rejects:

- raw audio/video and subtitle files;
- SQLite evidence databases;
- transcript-named artifacts;
- credential-like text in any bounded UTF text artifact, including JSON and unknown text extensions;
- `file:` links, URL userinfo, sensitive or expiring query parameters, and private absolute paths;
- broken or escaping relative links;
- provenance claims with missing or unknown evidence.

The textual distribution scan is deterministic and offline. It scans at most 4,096 files, 2 MiB per text file, and 16 MiB of text in total, and fails closed when those limits are exceeded. JSON is parsed and canonically serialized for a second scan so escaped URLs cannot bypass detection. Findings never echo the matched credential.

Visual candidates must reference same-source visual evidence IDs already retained by their semantic units; agents cannot supply arbitrary source paths. Candidate inputs must resolve to regular JPEG, PNG, or WebP files inside the evidence workspace without crossing a symbolic link. Inputs are capped at 20 MiB, 4096 pixels on either axis, and 16,000,000 pixels total; animation and unsupported decoded formats are rejected. The engine applies only normalized crops or bounded two-to-four-frame compositions, decodes and re-encodes the result as metadata-free PNG, records its SHA-256 digest, and enforces a 20 MiB total output bound. Author selections are rejected unless their linked claims preserve the exact visual or temporal evidence and every consuming artifact links the portable asset.

Sparse screenshots and synthesized notes can still be restricted by copyright, contract, privacy, or platform terms. The tool records attribution and minimizes copied content, but users remain responsible for ensuring they have permission to create and distribute a particular skill.

## Optional code validation

`video-to-skill validate --check-code` performs parse-only validation for Python, JSON, shell, and JavaScript fences. It does not run the extracted program. Source-derived commands remain data, not build instructions.

Any future execution-level validator must be separately opt-in and run in an isolated environment with:

- no network;
- read-only source evidence;
- a fresh temporary working directory;
- CPU, memory, process, and wall-clock limits;
- no mounted user credentials or home directory.
