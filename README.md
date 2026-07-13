# grok-upload-guard

**Detect** whether [Grok Build CLI](https://x.ai/) has been uploading your local codebase, and **fix** (disable) whole-repo / session-trace uploads via `~/.grok/config.toml`.

Local-only. No network calls. No dependencies beyond Python 3.10+.

## Why this exists

Independent wire analysis (July 2026) and local logs show Grok Build CLI can:

1. Upload a **snapshot of your git-tracked repo** (including history) as a bundle / archive — even for files the agent never opens.
2. Send **file contents the agent reads** (including `.env` if read) to the model API — normal for cloud coding agents, but unredacted.
3. Keep uploads on even when **“Improve the model” is off** (that toggle is about training policy, not transport).

Uploads are product infrastructure (`repo_state` / session traces), not something the agent “decides” per reply.

**Impact:** private repos, API keys, DB passwords, and full git history may leave your machine. Treat any secrets that lived in a workspace you ran Grok Build on as **potentially exposed** and rotate them.

This tool addresses the **whole-repo / trace upload** channel. It does **not** stop the model API from receiving files the agent intentionally reads (that is how cloud agents work).

## Install

```bash
git clone https://github.com/reedchan7/grok-upload-guard.git
cd grok-upload-guard
chmod +x grok-upload-guard.py
```

## Usage

### 1. Detect (default)

```bash
python3 grok-upload-guard.py
# or
python3 grok-upload-guard.py detect
```

Example brief output:

```
Verdict: UPLOADED (local evidence of codebase/session uploads)
Grok 0.2.99  |  starts=89  enqueued=82  gcs_uploaded=188  repos=4
Decisions: enabled=49  disabled=4  reasons={'proxy': 49, 'feature_off': 4}
Latest decision: enabled=False  reason=feature_off  ts=...
Config: telemetry=ok  trace_upload=ok  disable_codebase_upload=ok
Repos:
  ~/Workspaces/w/my-app   starts=53  enq=53  3.00 MB  sess=9
```

Full report / JSON:

```bash
python3 grok-upload-guard.py detect --full
python3 grok-upload-guard.py detect --json
```

### 2. Fix

Patches `~/.grok/config.toml` (creates a timestamped backup first):

```bash
python3 grok-upload-guard.py fix
python3 grok-upload-guard.py fix --dry-run
```

Keys written:

```toml
[features]
telemetry = false
feedback = false

[telemetry]
trace_upload = false
mixpanel_enabled = false

[harness]
disable_codebase_upload = true
```

### 3. Detect then fix

```bash
python3 grok-upload-guard.py all
```

### After fixing

1. **Start a new Grok Build session** (settings apply at session start).
2. Re-run `detect` — look for `uploads_enabled=false` / no new `repo_state.upload.start`.
3. Optionally set env vars (some builds honor these more aggressively):

```bash
export GROK_TELEMETRY_ENABLED=0
export GROK_TELEMETRY_TRACE_UPLOAD=0
export GROK_WORKSPACE_DATA_COLLECTION_DISABLED=1
```

## What it scans

| Source | Signal |
|--------|--------|
| `~/.grok/logs/unified.jsonl` | `repo_state.upload.*`, `trace.upload.decision` |
| `~/.grok/sessions/**/signals.json` | `gcsQueueUploaded` / `gcsQueueEnqueued` |
| `~/.grok/config.toml` | privacy-related keys |
| `~/.grok/upload_queue/` | pending staged blobs |

Override home with `--home /path/to/.grok` or `GROK_HOME`.

## Limits (read this)

- **Local evidence only** — proves enqueue/upload counters on your machine, not remote bucket contents.
- **Does not block model-channel reads** — if the agent opens `.env`, that content still goes to the cloud model API.
- **Remote settings** may still influence upload decisions; re-check after every CLI update.
- **Not affiliated with xAI.** Behavior is version-specific; verify on your install.

## Background

- Wire analysis: [gist by cereblab](https://gist.github.com/cereblab/dc9a40bc26120f4540e4e09b75ffb547)
- Discussion: [r/LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/comments/1ut7tis/grok_build_cli_uploads_your_whole_repo_full_git/)

## License

MIT
