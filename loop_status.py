"""Read-only, opt-in projection of the local Agent AutoLoop status command."""
import json
import os
from pathlib import Path
import shutil
import subprocess

RULE = "潮汐之核进度只读梳理"
SOURCE = "Agent AutoLoop · 本机状态"
STATES = {"queued", "claimed", "running", "done", "failed", "cancelled", "canceled", "stopped", "retrying"}


def _short(value, limit=120):
    return value[:limit] if isinstance(value, str) else None


def project(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list) or not isinstance(payload.get("runs"), list):
        raise ValueError("invalid state")
    visible = lambda row: isinstance(row, dict) and (
        row.get("name") == RULE or row.get("rule") == RULE or
        (isinstance(row.get("tags"), list) and "tidecore" in row["tags"])
    )
    rules = [_short(row.get("name")) for row in payload["lines"] if visible(row) and isinstance(row.get("name"), str)]
    rows = []
    for row in payload["runs"]:
        if not visible(row) or type(row.get("id")) is not int or row["id"] < 1:
            continue
        state = row.get("state")
        rows.append({
            "id": row["id"], "rule": _short(row.get("rule")), "agent": _short(row.get("agent"), 40),
            "state": state if isinstance(state, str) and state in STATES else "unknown",

            "updated_at": _short(row.get("finishedAt") or row.get("claimedAt") or row.get("createdAt"), 40),
        })
    return {"status": "online", "source": SOURCE, "observed_at": _short(payload.get("now"), 40),
            "rules": rules[:60], "runs": rows[:60]}


def read_status():
    if os.environ.get("CAS_LOOP_PANEL") != "1":
        return {"status": "disabled", "source": SOURCE}
    node = shutil.which("node")
    home = Path.home() / ".agent-autoloop"
    entry = home / "app" / "bin" / "agent-autoloop.ts"
    if not node or not entry.is_file():
        return {"status": "unavailable", "source": SOURCE}
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("AGENT_AUTOLOOP_")}
    env.pop("NODE_OPTIONS", None)
    env["AGENT_AUTOLOOP_HOME"] = str(home)
    try:
        result = subprocess.run(
            [node, "--disable-warning=ExperimentalWarning", str(entry), "status", "--json"],
            cwd=str(entry.parent), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=4, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return project(json.loads(result.stdout))
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"status": "unavailable", "source": SOURCE}
