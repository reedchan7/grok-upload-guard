#!/usr/bin/env python3
"""
grok-upload-guard — detect + disable Grok Build CLI whole-repo uploads.

Local-only. Does not contact the network.

  python3 grok-upload-guard.py              # detect (brief)
  python3 grok-upload-guard.py detect
  python3 grok-upload-guard.py detect --full
  python3 grok-upload-guard.py fix          # patch ~/.grok/config.toml
  python3 grok-upload-guard.py fix --dry-run
  python3 grok-upload-guard.py all          # detect, then fix

See README.md for context and limits.
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

    for row in rows:
        msg = row.get("msg") or ""
        ctx = row.get("ctx") if isinstance(row.get("ctx"), dict) else {}
        sid = str(row.get("sid") or "")
        if msg == "repo_state.upload.start":
            starts += 1
            repo = ctx.get("repo_path") or "(unknown)"
            by_repo[repo]["starts"] += 1
            by_repo[repo]["sessions"].add(sid)
            turn_repo[(sid, ctx.get("turn_number"))] = repo
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
        elif msg == "trace.upload.decision":
            enabled = ctx.get("uploads_enabled")
            if enabled is True:
                decisions_true += 1
            elif enabled is False:
                decisions_false += 1
            reason = ctx.get("upload_reason")
            if reason:
                reasons[str(reason)] += 1
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
        "config": privacy,
        "config_path": str(cfg_file),
        "upload_queue_pending_files": queue_files,
        "upload_queue_pending_bytes": queue_bytes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _flag(val: Any, want: Any) -> str:
    if val is want:
        return "ok"
    if val is None:
        return "unset"
    return f"WARN({val!r})"


def _short(path: str, n: int = 52) -> str:
    home = str(Path.home())
    p = path.replace(home, "~") if path.startswith(home) else path
    return p if len(p) <= n else "…" + p[-(n - 1) :]


def render_brief(d: dict[str, Any]) -> str:
    lines = []
    if d["evidence_of_upload"]:
        lines.append("Verdict: UPLOADED (local evidence of codebase/session uploads)")
    else:
        lines.append("Verdict: no local codebase-upload evidence found")
    lines.append(
        f"Grok {d.get('grok_version') or '?'}  |  "
        f"starts={d['repo_state_upload_start']}  "
        f"enqueued={d['repo_state_upload_enqueued']}  "
        f"gcs_uploaded={d['gcs_queue_uploaded']}  "
        f"repos={len(d['repos'])}"
    )
    lines.append(
        f"Decisions: enabled={d['decisions_enabled']}  "
        f"disabled={d['decisions_disabled']}  "
        f"reasons={d.get('upload_reasons') or {}}"
    )
    ld = d.get("latest_decision") or {}
    if ld:
        lines.append(
            f"Latest decision: enabled={ld.get('uploads_enabled')}  "
            f"reason={ld.get('upload_reason')}  ts={ld.get('ts')}"
        )
    c = d["config"]
    lines.append(
        "Config: "
        f"telemetry={_flag(c.get('features.telemetry'), False)}  "
        f"trace_upload={_flag(c.get('telemetry.trace_upload'), False)}  "
        f"disable_codebase_upload={_flag(c.get('harness.disable_codebase_upload'), True)}"
    )
    lines.append(
        f"Upload queue pending: {d['upload_queue_pending_files']} files / "
        f"{d['upload_queue_pending_bytes']} B"
    )
    if d["repos"]:
        lines.append("Repos:")
        for r in d["repos"][:8]:
            lines.append(
                f"  {_short(r['path']):<52}  "
                f"starts={r['starts']:<3}  enq={r['enqueued']:<3}  "
                f"{r['mb']:>6.2f} MB  sess={r['sessions']}"
            )
        if len(d["repos"]) > 8:
            lines.append(f"  … +{len(d['repos']) - 8} more")
    lines.append("Tip:  python3 grok-upload-guard.py fix")
    lines.append("      python3 grok-upload-guard.py detect --full")
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
            "**Local evidence shows codebase/session uploads occurred.**"
            if d["evidence_of_upload"]
            else "**No local evidence of codebase uploads found.**"
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
        description="Detect and disable Grok Build CLI whole-repo uploads (local-only).",
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
