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

## Quick start (no clone)

```bash
# Detect — scan ~/.grok logs for whole-repo / session-trace upload evidence
curl -fsSL https://raw.githubusercontent.com/reedchan7/grok-upload-guard/main/grok-upload-guard.py | python3 - detect

# Fix — patch ~/.grok/config.toml (creates a timestamped backup)
curl -fsSL https://raw.githubusercontent.com/reedchan7/grok-upload-guard/main/grok-upload-guard.py | python3 - fix
```

Or clone:

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

## Sources & community discussion

Primary technical evidence and public conversation used for this tool (July 2026). Not exhaustive; behavior may change by CLI version.

### Wire analysis & forums

| Source | What it shows |
|--------|----------------|
| [cereblab — wire-level analysis (gist)](https://gist.github.com/cereblab/dc9a40bc26120f4540e4e09b75ffb547) | mitmproxy captures; git-bundle recovery of never-read canaries; multi-GB `/v1/storage` uploads; “Improve the model” does not stop transport |
| [r/LocalLLaMA thread](https://www.reddit.com/r/LocalLLaMA/comments/1ut7tis/grok_build_cli_uploads_your_whole_repo_full_git/) | Original public write-up of the findings |
| [Hacker News discussion](https://news.ycombinator.com/item?id=48877371) | Cross-community technical discussion |

### X — independent reports & amplification

| Post | Notes |
|------|--------|
| [@scaling01](https://x.com/scaling01/status/2076317838533398597) | Quotes the two core claims from the gist (whole-repo upload + unredacted `.env` when read) |
| [@joejo2038](https://x.com/joejo2038/status/2076220544475971612) | Full Korean summary of the wire analysis; high share volume |
| [@landiantech](https://x.com/landiantech/status/2076239493833842700) | Chinese tech coverage (default full-git upload + GCS) |
| [@xsser_w](https://x.com/xsser_w/status/2076234823115673611) | Flags the Reddit report; tags Elon |
| [@cccchuizi](https://x.com/cccchuizi/status/2076339595986473102) | Practical “do not run on sensitive trees” checklist + config mitigation |
| [@GrokInsider](https://x.com/GrokInsider/status/2076332493922332760) | Community report: intentional design vs disclosure gap; config knobs |
| [@XBToshi](https://x.com/XBToshi/status/2076338252051841512) | Strong privacy critique (“spyware” framing); asks for explanation |
| [@_nilni](https://x.com/_nilni/status/2076461794735071674) | Asks @grok to confirm the Reddit analysis |

### X — @grok responses (public admissions of mechanics)

These are replies from the **@grok** account (not a formal xAI PR blog post). They still publicly accept the wire findings:

| Post | Notes |
|------|--------|
| [@grok](https://x.com/grok/status/2076368434699403395) | “Yes, the gist's wire analysis is accurate… full git-tracked repo… Improve the model toggle doesn't disable it… by design” |
| [@grok](https://x.com/grok/status/2076316408036618638) | Confirms full Git bundle upload to GCS by default; enterprise ZDR mentioned |
| [@grok](https://x.com/grok/status/2076276290223591804) | Chinese confirmation of whole-repo git-bundle upload + Improve-the-model ineffective |
| [@grok](https://x.com/grok/status/2076466539469865363) | States upload is by design; points to `harness.disable_codebase_upload` / `telemetry.trace_upload` |
| [@grok](https://x.com/grok/status/2076461985340788937) | Confirms full git-tracked repo upload; Improve-the-model controls training only |

**Note:** As of those discussions, **@elonmusk** and the **@xai** product account had not posted a dedicated rebuttal of the upload analysis; @grok replies were the main public acknowledgment on X.

## License

MIT
