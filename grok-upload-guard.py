#!/usr/bin/env python3
"""
grok-upload-guard — local self-defense helper for Grok Build CLI users.

This tool does NOT claim to re-prove the upload behavior with packet capture.
That was established by independent wire analysis (community research).
Here we only:
  - detect: scan local ~/.grok logs/signals for upload evidence on THIS machine
  - fix:    patch ~/.grok/config.toml to disable whole-repo / session-trace uploads

Local-only. Does not contact the network.

  python3 grok-upload-guard.py              # detect (brief)
  python3 grok-upload-guard.py detect
  python3 grok-upload-guard.py detect --full
  python3 grok-upload-guard.py fix          # patch ~/.grok/config.toml
  python3 grok-upload-guard.py fix --dry-run
  python3 grok-upload-guard.py all          # detect, then fix

See README.md for context, sources, and limits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME", Path.home() / ".grok")).expanduser()


def config_path(home: Path | None = None) -> Path:
    return (home or grok_home()) / "config.toml"


# ---------------------------------------------------------------------------
# Minimal TOML helpers (no third-party deps)
# ---------------------------------------------------------------------------

def parse_toml_simple(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    section: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]") and "=" not in line:
            body = line[1:-1].strip()
            if body.startswith("["):
                section = []
                continue
            section = [p.strip() for p in body.split(".")]
            cur: Any = result
            for part in section:
                cur = cur.setdefault(part, {})
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.split("#", 1)[0].strip()
        if val.startswith("[") and not val.endswith("]"):
            chunks = [val]
            while i < len(lines):
                nxt = lines[i].split("#", 1)[0].strip()
                i += 1
                if not nxt:
                    continue
                chunks.append(nxt)
                if nxt.endswith("]"):
                    break
            val = " ".join(chunks)
        if val in ("true", "false"):
            parsed: Any = val == "true"
        elif (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            parsed = val[1:-1]
        else:
            parsed = val
        cur = result
        for part in section:
            cur = cur.setdefault(part, {})
        if isinstance(cur, dict):
            cur[key] = parsed
    return result


def get_nested(cfg: dict[str, Any], *keys: str) -> Any:
    cur: Any = cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


# Privacy keys we want (documented + binary-confirmed for Grok Build CLI).
FIX_SPEC: list[tuple[str, str, str]] = [
    # (section, key, value_literal)
    ("features", "telemetry", "false"),
    ("features", "feedback", "false"),
    ("telemetry", "trace_upload", "false"),
    ("telemetry", "mixpanel_enabled", "false"),
    ("harness", "disable_codebase_upload", "true"),
]


def set_toml_bool(text: str, section: str, key: str, value_lit: str) -> str:
    """Ensure [section] key = value_lit in a flat TOML file. Conservative patcher."""
    # Match [section] header (not array-of-tables)
    sec_re = re.compile(
        rf"(?m)^\[{re.escape(section)}\]\s*$"
    )
    key_re = re.compile(
        rf"(?m)^(\s*{re.escape(key)}\s*=\s*)([^\n#]+?)(\s*(?:#.*)?)?$"
    )

    m = sec_re.search(text)
    if not m:
        block = (
            f"\n# added by grok-upload-guard\n"
            f"[{section}]\n"
            f"{key} = {value_lit}\n"
        )
        return text.rstrip() + "\n" + block

    # Find end of this section (next [header] or EOF)
    start = m.end()
    next_sec = re.search(r"(?m)^\[", text[start:])
    end = start + next_sec.start() if next_sec else len(text)
    body = text[start:end]
    km = key_re.search(body)
    if km:
        new_body = (
            body[: km.start()]
            + f"{km.group(1)}{value_lit}"
            + (km.group(3) or "")
            + body[km.end() :]
        )
    else:
        insert = f"{key} = {value_lit}\n"
        if body.startswith("\n"):
            new_body = "\n" + insert + body[1:]
        else:
            new_body = "\n" + insert + body
    return text[:start] + new_body + text[end:]


def apply_fix(text: str) -> str:
    out = text
    if not out.endswith("\n"):
        out += "\n"
    for section, key, value_lit in FIX_SPEC:
        out = set_toml_bool(out, section, key, value_lit)
    return out


# ---------------------------------------------------------------------------
# Detect
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_auth_privacy(home: Path) -> dict[str, Any]:
    """Read only privacy/opt-out fields from auth.json (never log tokens)."""
    path = home / "auth.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    keys = [
        "coding_data_retention_opt_out",
        "data_retention_opt_out",
        "product_data_retention_opt_out",
    ]
    out: dict[str, Any] = {}
    for k in keys:
        if k in data:
            out[k] = data[k]
    # Grok auth.json nests account data under keys like "https://auth.x.ai::<uuid>".
    for v in data.values():
        if isinstance(v, dict):
            for k in keys:
                if k in v:
                    out[k] = v[k]
    return out


def parse_ts(ts: Any) -> datetime | None:
    """Best-effort parse a timestamp from Grok logs (ISO str or ms/s epoch)."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        # Grok logs often use millisecond epochs.
        if ts > 1e12:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(ts, str):
        ts = ts.strip()
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass
        try:
            f = float(ts)
            if f > 1e12:
                f = f / 1000.0
            return datetime.fromtimestamp(f, tz=timezone.utc)
        except ValueError:
            return None
    return None


def detect(home: Path) -> dict[str, Any]:
    log_path = home / "logs" / "unified.jsonl"
    rows = load_jsonl(log_path)

    starts = 0
    enqueued = 0
    enq_bytes = 0
    by_repo: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"starts": 0, "enqueued": 0, "bytes": 0, "sessions": set()}
    )
    decisions_true = 0
    decisions_false = 0
    reasons: Counter[str] = Counter()
    latest_decision: dict[str, Any] | None = None
    # session -> turn -> repo from starts
    turn_repo: dict[tuple[str, Any], str] = {}

    today = datetime.now(timezone.utc).astimezone().date()
    today_starts = 0
    today_enqueued = 0
    today_enq_bytes = 0
    today_decisions_true = 0
    today_decisions_false = 0
    today_reasons: Counter[str] = Counter()
    today_by_repo: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"starts": 0, "enqueued": 0, "bytes": 0, "sessions": set()}
    )

    for row in rows:
        row_ts = parse_ts(row.get("ts"))
        is_today = row_ts is not None and row_ts.astimezone().date() == today
        msg = row.get("msg") or ""
        ctx = row.get("ctx") if isinstance(row.get("ctx"), dict) else {}
        sid = str(row.get("sid") or "")
        if msg == "repo_state.upload.start":
            starts += 1
            repo = ctx.get("repo_path") or "(unknown)"
            by_repo[repo]["starts"] += 1
            by_repo[repo]["sessions"].add(sid)
            turn_repo[(sid, ctx.get("turn_number"))] = repo
            if is_today:
                today_starts += 1
                today_by_repo[repo]["starts"] += 1
                today_by_repo[repo]["sessions"].add(sid)
        elif msg == "repo_state.upload.enqueued":
            enqueued += 1
            size = int(ctx.get("size_bytes") or 0)
            enq_bytes += size
            repo = ctx.get("repo_path") or turn_repo.get(
                (sid, ctx.get("turn_number"))
            ) or "(unknown)"
            by_repo[repo]["enqueued"] += 1
            by_repo[repo]["bytes"] += size
            by_repo[repo]["sessions"].add(sid)
            if is_today:
                today_enqueued += 1
                today_enq_bytes += size
                today_by_repo[repo]["enqueued"] += 1
                today_by_repo[repo]["bytes"] += size
                today_by_repo[repo]["sessions"].add(sid)
        elif msg == "trace.upload.decision":
            enabled = ctx.get("uploads_enabled")
            if enabled is True:
                decisions_true += 1
                if is_today:
                    today_decisions_true += 1
            elif enabled is False:
                decisions_false += 1
                if is_today:
                    today_decisions_false += 1
            reason = ctx.get("upload_reason")
            if reason:
                reasons[str(reason)] += 1
                if is_today:
                    today_reasons[str(reason)] += 1
            latest_decision = {
                "ts": row.get("ts"),
                "uploads_enabled": enabled,
                "upload_reason": reason,
                "trace_upload": ctx.get("trace_upload"),
                "session_id": sid,
            }

    # session signals
    gcs_uploaded = 0
    gcs_enqueued = 0
    sessions_hit = 0
    sessions_root = home / "sessions"
    if sessions_root.is_dir():
        for project in sessions_root.iterdir():
            if not project.is_dir():
                continue
            for sess in project.iterdir():
                sig = sess / "signals.json"
                if not sig.is_file():
                    continue
                try:
                    data = json.loads(sig.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                up = int(data.get("gcsQueueUploaded") or 0)
                enq = int(data.get("gcsQueueEnqueued") or 0)
                if up or enq:
                    sessions_hit += 1
                    gcs_uploaded += up
                    gcs_enqueued += enq

    # config
    cfg_file = config_path(home)
    cfg: dict[str, Any] = {}
    if cfg_file.is_file():
        try:
            cfg = parse_toml_simple(cfg_file.read_text(encoding="utf-8"))
        except OSError:
            cfg = {}

    auth_privacy = load_auth_privacy(home)

    privacy = {
        "features.telemetry": get_nested(cfg, "features", "telemetry"),
        "telemetry.trace_upload": get_nested(cfg, "telemetry", "trace_upload"),
        "harness.disable_codebase_upload": get_nested(
            cfg, "harness", "disable_codebase_upload"
        ),
        "telemetry.mixpanel_enabled": get_nested(cfg, "telemetry", "mixpanel_enabled"),
    }

    ver = None
    vf = home / "version.json"
    if vf.is_file():
        try:
            ver = json.loads(vf.read_text()).get("version")
        except (OSError, json.JSONDecodeError):
            pass

    uq = home / "upload_queue"
    queue_files = 0
    queue_bytes = 0
    if uq.is_dir():
        for p in uq.rglob("*"):
            if p.is_file():
                queue_files += 1
                queue_bytes += p.stat().st_size

    repos_out = []
    for repo, d in sorted(by_repo.items(), key=lambda kv: -kv[1]["starts"]):
        repos_out.append(
            {
                "path": repo,
                "starts": d["starts"],
                "enqueued": d["enqueued"],
                "mb": round(d["bytes"] / (1024 * 1024), 3),
                "sessions": len(d["sessions"]),
            }
        )

    today_repos_out = []
    for repo, d in sorted(today_by_repo.items(), key=lambda kv: -kv[1]["starts"]):
        today_repos_out.append(
            {
                "path": repo,
                "starts": d["starts"],
                "enqueued": d["enqueued"],
                "mb": round(d["bytes"] / (1024 * 1024), 3),
                "sessions": len(d["sessions"]),
            }
        )

    evidence = bool(starts or enqueued or gcs_uploaded)

    return {
        "grok_home": str(home),
        "grok_version": ver,
        "log_exists": log_path.is_file(),
        "evidence_of_upload": evidence,
        "repo_state_upload_start": starts,
        "repo_state_upload_enqueued": enqueued,
        "enqueued_mb": round(enq_bytes / (1024 * 1024), 3),
        "decisions_enabled": decisions_true,
        "decisions_disabled": decisions_false,
        "upload_reasons": dict(reasons),
        "latest_decision": latest_decision,
        "gcs_queue_uploaded": gcs_uploaded,
        "gcs_queue_enqueued": gcs_enqueued,
        "sessions_with_gcs": sessions_hit,
        "repos": repos_out,
        "auth_privacy": auth_privacy,
        "today": {
            "date": today.isoformat(),
            "repo_state_upload_start": today_starts,
            "repo_state_upload_enqueued": today_enqueued,
            "enqueued_mb": round(today_enq_bytes / (1024 * 1024), 3),
            "decisions_enabled": today_decisions_true,
            "decisions_disabled": today_decisions_false,
            "upload_reasons": dict(today_reasons),
            "repos": today_repos_out,
        },
        "config": privacy,
        "config_path": str(cfg_file),
        "upload_queue_pending_files": queue_files,
        "upload_queue_pending_bytes": queue_bytes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _opt_out_flag(val: Any) -> str:
    if val is True:
        return "opted_out"
    if val is False:
        return "not_opted_out"
    return "unset"


def _short(path: str, n: int = 52) -> str:
    home = str(Path.home())
    p = path.replace(home, "~") if path.startswith(home) else path
    return p if len(p) <= n else "…" + p[-(n - 1) :]


def render_brief(d: dict[str, Any]) -> str:
    lines: list[str] = []

    # Header
    generated = d.get("generated_at") or datetime.now(timezone.utc).isoformat()
    if generated:
        generated = generated[:19].replace("T", " ")
    lines.append(f"Grok upload guard — {generated} UTC")
    lines.append(
        f"Version: {d.get('grok_version') or '?'}  |  Home: {_short(d['grok_home'])}"
    )
    lines.append("")

    # Verdict
    if d["evidence_of_upload"]:
        lines.append("[Verdict]")
        lines.append("Local logs show codebase/session uploads on this machine.")
    else:
        lines.append("[Verdict]")
        lines.append("No local codebase-upload evidence found.")
    lines.append("")

    # Today
    t = d.get("today") or {}
    lines.append(f"[Today: {t.get('date')}]")
    lines.append(f"  {'Upload starts:':<18} {t.get('repo_state_upload_start', 0)}")
    lines.append(
        f"  {'Upload enqueued:':<18} {t.get('repo_state_upload_enqueued', 0)} "
        f"({t.get('enqueued_mb', 0)} MB)"
    )
    lines.append(
        f"  {'Decisions:':<18} +{t.get('decisions_enabled', 0)} enabled / "
        f"+{t.get('decisions_disabled', 0)} disabled"
    )
    reasons_today = t.get("upload_reasons") or {}
    if reasons_today:
        lines.append(f"  {'Reasons:':<18} {reasons_today}")
    today_repos = t.get("repos") or []
    if today_repos:
        lines.append(f"  {'Repos affected:':<18} {len(today_repos)}")
        for r in today_repos[:5]:
            lines.append(
                f"    - {_short(r['path']):<48}  "
                f"starts={r['starts']:<3}  enq={r['enqueued']:<3}  "
                f"{r['mb']:>6.2f} MB"
            )
        if len(today_repos) > 5:
            lines.append(f"    ... +{len(today_repos) - 5} more")
    else:
        lines.append(f"  {'Repos affected:':<18} none")
    lines.append("")

    # All time
    lines.append("[All time]")
    lines.append(f"  {'Upload starts:':<18} {d['repo_state_upload_start']}")
    lines.append(
        f"  {'Upload enqueued:':<18} {d['repo_state_upload_enqueued']} ({d['enqueued_mb']} MB)"
    )
    lines.append(f"  {'GCS uploaded:':<18} {d['gcs_queue_uploaded']}")
    lines.append(
        f"  {'Decisions:':<18} {d['decisions_enabled']} enabled / "
        f"{d['decisions_disabled']} disabled"
    )
    reasons = d.get("upload_reasons") or {}
    if reasons:
        lines.append(f"  {'Reasons:':<18} {reasons}")
    ld = d.get("latest_decision") or {}
    if ld:
        status = "enabled" if ld.get("uploads_enabled") else "disabled"
        lines.append(
            f"  {'Latest decision:':<18} {status} ({ld.get('upload_reason')}) at {ld.get('ts')}"
        )
    lines.append("")

    # Privacy opt-out
    lines.append("[Privacy opt-out]")
    ap = d.get("auth_privacy") or {}
    if ap:
        for k, v in ap.items():
            lines.append(f"  {k}: {_opt_out_flag(v)}")
    else:
        lines.append("  (not found in auth.json)")
    lines.append("")

    # Config
    lines.append("[Config]")
    c = d["config"]

    def _cfg(val: Any) -> str:
        if val is None:
            return "unset"
        if isinstance(val, bool):
            return "true" if val else "false"
        return str(val)

    lines.append(
        f"  {'telemetry:':<26} {_cfg(c.get('features.telemetry'))}"
    )
    lines.append(
        f"  {'trace_upload:':<26} {_cfg(c.get('telemetry.trace_upload'))}"
    )
    lines.append(
        f"  {'disable_codebase_upload:':<26} "
        f"{_cfg(c.get('harness.disable_codebase_upload'))}"
    )
    lines.append("")

    # Upload queue
    lines.append("[Upload queue]")
    lines.append(
        f"  Pending: {d['upload_queue_pending_files']} files / "
        f"{d['upload_queue_pending_bytes']} B"
    )
    lines.append("")

    # Repos
    if d["repos"]:
        lines.append("[Repos]")
        for r in d["repos"][:8]:
            lines.append(
                f"  {_short(r['path']):<52}  "
                f"starts={r['starts']:<3}  enq={r['enqueued']:<3}  "
                f"{r['mb']:>6.2f} MB  sess={r['sessions']}"
            )
        if len(d["repos"]) > 8:
            lines.append(f"  ... +{len(d['repos']) - 8} more")
        lines.append("")

    lines.append(
        "Tip: run `python3 grok-upload-guard.py fix` to patch config, "
        "or `--full` for details."
    )
    return "\n".join(lines) + "\n"


def render_full(d: dict[str, Any]) -> str:
    lines = [
        "# Grok Build upload audit",
        "",
        f"- Generated (UTC): `{d['generated_at']}`",
        f"- Grok home: `{d['grok_home']}`",
        f"- Grok version: `{d.get('grok_version')}`",
        f"- Config: `{d['config_path']}`",
        "",
        "## Verdict",
        "",
        (
            "**Local `~/.grok` logs show codebase/session uploads on this machine.** "
            "(Local detection only — the original proof is community wire capture; "
            "see README Sources.)"
            if d["evidence_of_upload"]
            else "**No local codebase-upload evidence found on this machine.** "
            "(Does not disprove independent wire analysis.)"
        ),
        "",
        f"- `repo_state.upload.start`: **{d['repo_state_upload_start']}**",
        f"- `repo_state.upload.enqueued`: **{d['repo_state_upload_enqueued']}** "
        f"({d['enqueued_mb']} MB logged)",
        f"- decisions uploads_enabled=true: **{d['decisions_enabled']}**",
        f"- decisions uploads_enabled=false: **{d['decisions_disabled']}**",
        f"- session gcsQueueUploaded total: **{d['gcs_queue_uploaded']}** "
        f"(sessions with activity: {d['sessions_with_gcs']})",
        f"- upload reasons: `{d.get('upload_reasons')}`",
        "",
        "## Today",
        "",
    ]
    t = d.get("today") or {}
    lines += [
        f"- Date: **{t.get('date')}**",
        f"- `repo_state.upload.start`: **{t.get('repo_state_upload_start', 0)}**",
        f"- `repo_state.upload.enqueued`: **{t.get('repo_state_upload_enqueued', 0)}** "
        f"({t.get('enqueued_mb', 0)} MB logged)",
        f"- decisions uploads_enabled=true: **{t.get('decisions_enabled', 0)}**",
        f"- decisions uploads_enabled=false: **{t.get('decisions_disabled', 0)}**",
        f"- upload reasons today: `{t.get('upload_reasons')}`",
        "",
        "### Repos today",
        "",
    ]
    if not t.get("repos"):
        lines.append("_None._")
    else:
        lines.append("| Repo | Starts | Enqueued | MB | Sessions |")
        lines.append("|------|-------:|---------:|---:|---------:|")
        for r in t["repos"]:
            lines.append(
                f"| `{r['path']}` | {r['starts']} | {r['enqueued']} | "
                f"{r['mb']} | {r['sessions']} |"
            )
    lines += [
        "",
        "## Privacy opt-out",
        "",
        "Status read from `~/.grok/auth.json` (tokens are never logged).",
        "",
    ]
    ap = d.get("auth_privacy") or {}
    if ap:
        for k, v in ap.items():
            lines.append(f"- `{k}`: **`{_opt_out_flag(v)}`**")
    else:
        lines.append("_No opt-out fields found in auth.json._")
    lines += [
        "",
        "## Config",
        "",
        "```json",
        json.dumps(d["config"], indent=2),
        "```",
        "",
        "## Repos",
        "",
    ]
    if not d["repos"]:
        lines.append("_None._")
    else:
        lines.append("| Repo | Starts | Enqueued | MB | Sessions |")
        lines.append("|------|-------:|---------:|---:|---------:|")
        for r in d["repos"]:
            lines.append(
                f"| `{r['path']}` | {r['starts']} | {r['enqueued']} | "
                f"{r['mb']} | {r['sessions']} |"
            )
    lines += [
        "",
        "## Latest decision",
        "",
        "```json",
        json.dumps(d.get("latest_decision"), indent=2),
        "```",
        "",
        "## Limits",
        "",
        "- Not a packet-capture proof; community wire analysis is the discovery source.",
        "- Local logs only — does not re-download remote objects.",
        "- Files the agent *reads* still go to the model API (normal for cloud agents).",
        "- This tool only targets whole-repo / session-trace upload (Channel B).",
        "- Start a **new** Grok session after `fix` and re-run `detect`.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fix
# ---------------------------------------------------------------------------

def fix_config(home: Path, dry_run: bool = False) -> tuple[bool, str]:
    path = config_path(home)
    if not path.exists():
        text = (
            "# created by grok-upload-guard\n"
            "[features]\n"
            "telemetry = false\n"
            "feedback = false\n"
            "\n"
            "[telemetry]\n"
            "trace_upload = false\n"
            "mixpanel_enabled = false\n"
            "\n"
            "[harness]\n"
            "disable_codebase_upload = true\n"
        )
        if dry_run:
            return True, f"[dry-run] would create {path}\n\n{text}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True, f"Created {path} with privacy defaults."

    original = path.read_text(encoding="utf-8")
    updated = apply_fix(original)
    if updated == original:
        return True, f"Already configured: {path}"

    if dry_run:
        return True, f"[dry-run] would patch {path} ({len(original)} → {len(updated)} bytes)"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"config.toml.bak.{stamp}")
    shutil.copy2(path, backup)
    path.write_text(updated, encoding="utf-8")
    return True, f"Patched {path}\nBackup: {backup}"


def cmd_detect(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser() if args.home else grok_home()
    if not home.is_dir():
        print(f"Grok home not found: {home}", file=sys.stderr)
        return 1
    data = detect(home)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if args.full:
        print(render_full(data))
    else:
        print(render_brief(data), end="")
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser() if args.home else grok_home()
    ok, msg = fix_config(home, dry_run=args.dry_run)
    print(msg)
    if not ok:
        return 1
    if not args.dry_run:
        print()
        print("Next:")
        print("  1. Start a NEW Grok Build session (config is read at session start).")
        print("  2. Re-run:  python3 grok-upload-guard.py detect")
        print("  3. Expect latest decision uploads_enabled=false, and no new starts.")
        print()
        print("Optional env (stronger than config for some builds):")
        print("  export GROK_TELEMETRY_ENABLED=0")
        print("  export GROK_TELEMETRY_TRACE_UPLOAD=0")
        print("  export GROK_WORKSPACE_DATA_COLLECTION_DISABLED=1")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    rc = cmd_detect(args)
    print()
    print("--- fix ---")
    rc2 = cmd_fix(args)
    return rc or rc2


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="grok-upload-guard",
        description=(
            "Local self-defense helper: scan ~/.grok for upload evidence and "
            "disable whole-repo/session-trace uploads. Not a wire-capture proof "
            "(that research is from the community; see README)."
        ),
    )
    ap.add_argument(
        "--home",
        default=None,
        help="Grok home (default: $GROK_HOME or ~/.grok)",
    )
    sub = ap.add_subparsers(dest="cmd")

    p_det = sub.add_parser("detect", help="Scan local logs for upload evidence (default)")
    p_det.add_argument("--full", action="store_true", help="Full markdown report")
    p_det.add_argument("--json", action="store_true", help="JSON output")

    p_fix = sub.add_parser("fix", help="Patch ~/.grok/config.toml to disable uploads")
    p_fix.add_argument("--dry-run", action="store_true", help="Show what would change")

    p_all = sub.add_parser("all", help="Detect, then fix")
    p_all.add_argument("--full", action="store_true")
    p_all.add_argument("--json", action="store_true")
    p_all.add_argument("--dry-run", action="store_true")

    # also allow top-level --full / --json when no subcommand
    ap.add_argument("--full", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)

    args = ap.parse_args()
    if args.cmd is None:
        args.cmd = "detect"
        # ensure attributes exist
        if not hasattr(args, "full"):
            args.full = False
        if not hasattr(args, "json"):
            args.json = False

    if args.cmd == "detect":
        return cmd_detect(args)
    if args.cmd == "fix":
        return cmd_fix(args)
    if args.cmd == "all":
        return cmd_all(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
