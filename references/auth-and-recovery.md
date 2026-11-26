# Authentication, Secrets & Failure Recovery

## Authentication and Secrets

When a source requires login, negotiate authentication once for the run: offer a named browser/profile, a local Netscape `cookies.txt`, or public items only. Set `VIDEO_TO_SKILL_COOKIES_FROM_BROWSER` only for the engine invocation and never ask for an account password.

- For browser auth, the engine decrypts cookies once into a private temporary snapshot and reuses isolated copies across workers, deleting them on exit. A supplied `cookies.txt` is snapshotted rather than modified in place.
- On macOS, a keychain prompt may appear once. Repeated prompts during one invocation indicate a failed auth session—stop and report the failure instead of retrying endlessly.
- Treat cookies, headers, tokens, expiring URLs, and browser snapshots as runtime secrets. Never quote them back, put them in task packets or logs, persist them in the workspace, or include them in the generated Skill.
- The engine records sanitized tool provenance directly at subprocess/provider boundaries in SQLite. Use `tool-runs WORKSPACE` only when a maintainer needs the sanitized JSONL under `WORKSPACE/logs/`. Raw stdout/stderr, env vars, credentials, signed URLs, private paths, and tool records never enter the portable Skill.

## Failure and Recovery

- **Keep the workspace** after success or failure. Never delete it to recover from an interrupted stage; `clean` is always an explicit user action.
- **Resume the same workspace** (`run --workspace WORKSPACE`) without retransmitting sources, changing output paths, or creating a replacement workspace to bypass a rejected task or conflict.
- **Rejected submission**: preserve task output, report the exact schema, lease, digest, evidence-scope, or snapshot error, and fix or redispatch only the affected durable task without relaying large payloads through the main agent.
- **Partial source failure**: continue remaining accessible sources and let the coverage ledger record the gap. If all useful sources are inaccessible, stop after persisting acquisition state and explain what auth or source change is needed.
- **Validation, compilation, or name conflict**: retain the workspace and staging artifacts, report the failing gate, and resume after repair. For same-name installation conflicts, use the coordinator's `ask-user` naming decision—never silently overwrite different content.
- **Refreshing inventory**: `--refresh` re-inspects sources; when duration, items, chapters, captions, or density signals change, the engine refreshes the analysis-depth contract and invalidates only affected stage snapshots.
