"""Explicit, metadata-only adapter for the official installed Codex App Server.

Only initialize, thread/list(useStateDbOnly=True), and thread/loaded/list are allowed.
Never resumes threads, reads message bodies, starts turns, approves tools, or sends input.
"""
import copy
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

METHODS = frozenset({"initialize", "thread/list", "thread/loaded/list"})
SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
                "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]
NOTE = ("Real stored session metadata from the official API. notLoaded means not loaded in "
        "THIS short-lived adapter process, NOT stopped or idle in the desktop/another process. "
        "Metadata only: no thread resume, message history, model run, or message delivery.")
_CACHE = {}
_QUARANTINED = {}
_LOCK = threading.Lock()


class CleanupBlockedError(OSError):
    pass


class CodexClient:
    """One owned stdio process. An allowlist prevents accidental control RPCs."""
    def __init__(self, cli, timeout=8):
        self.deadline = time.monotonic() + timeout
        self.serial = 0
        self.lines = queue.Queue()
        self.process = subprocess.Popen(
            [str(cli), "app-server", "--listen", "stdio://", "-c", 'sandbox_mode="read-only"'],
            cwd=str(Path(__file__).resolve().parent),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.readers = [threading.Thread(target=self._stdout, daemon=True),
                        threading.Thread(target=self._stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def _stdout(self):
        try:
            for line in self.process.stdout:
                if len(line) > 4_000_000:
                    self.lines.put(ValueError("App Server response exceeds metadata limit"))
                    return
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def _stderr(self):
        # Do not retain startup tracing, account details, or private paths in this app.
        for _ in self.process.stderr:
            pass

    def request(self, method, params):
        if method not in METHODS:
            raise ValueError("read-only adapter RPC is not allowed: " + str(method))
        self.serial += 1
        request_id = self.serial
        self.process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("metadata API timed out")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("metadata API timed out") from None
            if line is None:
                raise OSError("official App Server exited before replying")
            if isinstance(line, Exception):
                raise line
            try:
                value = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(value, dict):
                continue
            if "method" in value and "id" in value:
                # Never approve a server-initiated action or supply credentials.
                self.process.stdin.write(json.dumps({"id": value["id"], "error": {
                    "code": -32601, "message": "metadata-only client does not handle actions"}}) + "\n")
                self.process.stdin.flush()
                continue
            if value.get("id") != request_id:
                continue
            if "error" in value:
                error = value["error"]
                message = error.get("message", "API error") if isinstance(error, dict) else "API error"
                raise ValueError(str(message)[:500])
            result = value.get("result")
            if not isinstance(result, dict):
                raise ValueError("invalid metadata API result")
            return result

    def initialize(self):
        self.request("initialize", {"clientInfo": {"name": "local_team_sync_metadata",
                     "title": "Local Team Sync metadata-only adapter", "version": "0.2.0"}})
        self.process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        self.process.stdin.flush()

    def close(self):
        # EOF lets the official server shut down its own helpers before any fallback.
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()  # Only this adapter's child, never the desktop/daemon.
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for reader in self.readers:
            reader.join(timeout=0.5)
        incomplete = False
        for reader, stream in zip(self.readers, (self.process.stdout, self.process.stderr)):
            if reader.is_alive():
                # TextIO.close would wait indefinitely for a reader's lock if an inherited
                # helper pipe remains open. Do not synchronously close that stream.
                incomplete = True
                continue
            try:
                stream.close()
            except OSError:
                pass
        if incomplete:
            raise CleanupBlockedError("helper still holds an adapter pipe; native probes disabled until this app restarts")


def _text(value, limit=200):
    return value[:limit] if isinstance(value, str) else None


def _time(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _row(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None
    status = raw.get("status")
    status = status if isinstance(status, dict) else {}
    return {"id": raw["id"][:200], "name": _text(raw.get("name"), 500) or "(untitled session)",
            "cwd": _text(raw.get("cwd"), 1000),
            "source_kind": _text(raw.get("source"), 80) or "structured/unknown",
            "reported_runtime_status": _text(status.get("type"), 80) or "unknown",
            "created_at": _time(raw.get("createdAt")), "updated_at": _time(raw.get("updatedAt")),
            "can_send": False, "can_steer": False,
            "evidence": "metadata status in this adapter process only; not cross-process liveness"}


def _read(cli, scope_cwd, timeout=8):
    result = {"available": False, "tasks": [], "threads": [], "can_send": False, "can_steer": False,
              "adapter_mode": "metadata-only-stdio", "scope_cwd": str(scope_cwd),
              "status_scope": "this short-lived adapter process only", "note": NOTE}
    client = None
    try:
        cli, scope_cwd = Path(cli).resolve(), Path(scope_cwd).resolve()
        if not cli.is_file():
            raise ValueError("explicit official Codex CLI executable is missing")
        if os.name == "nt" and cli.suffix.lower() != ".exe":
            raise ValueError("Windows adapter requires the official codex.exe path")
        client = CodexClient(cli, timeout)
        client.initialize()
        rows = {}
        cursor = None
        truncated = False
        for page in range(4):  # At most 200 records from this explicitly scoped project.
            params = {"limit": 50, "sortKey": "updated_at", "modelProviders": [],
                      "sourceKinds": SOURCE_KINDS, "cwd": str(scope_cwd), "useStateDbOnly": True}
            if cursor is not None:
                params["cursor"] = cursor
            response = client.request("thread/list", params)
            if not isinstance(response.get("data"), list):
                raise ValueError("thread/list did not return metadata rows")
            for raw in response["data"]:
                item = _row(raw)
                # Enforce exact scope even if a server ignores or regresses its cwd filter.
                exact_scope = (item and item["cwd"] and os.path.isabs(item["cwd"]) and
                               os.path.normcase(os.path.normpath(item["cwd"])) ==
                               os.path.normcase(os.path.normpath(str(scope_cwd))))
                if exact_scope:
                    old = rows.get(item["id"])
                    # Some store migrations expose the same ID twice; keep newest metadata.
                    if old is None or (item["updated_at"] or 0, item["created_at"] or 0) > (old["updated_at"] or 0, old["created_at"] or 0):
                        rows[item["id"]] = item
            cursor = response.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str):
                raise ValueError("invalid thread/list cursor")
            truncated = page == 3
        loaded = client.request("thread/loaded/list", {})
        if not isinstance(loaded.get("data"), list):
            raise ValueError("invalid loaded-thread metadata")
        result.update(available=True, threads=sorted(rows.values(), key=lambda r: r["updated_at"] or 0, reverse=True),
                      loaded_count_in_adapter=len(loaded["data"]), truncated=truncated)
    except (ValueError, TypeError, RecursionError, OSError, subprocess.SubprocessError) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[:500]
    finally:
        if client is not None:
            try:
                client.close()
            except (OSError, subprocess.SubprocessError) as exc:
                _QUARANTINED[str(cli)] = str(exc)[:300]
                result.update(available=False, threads=[], error="adapter cleanup failed: " + str(exc)[:300])
    result["observed_at"] = time.time()
    return result


def read_codex_threads(cli, scope_cwd, timeout=8, cache_seconds=15):
    """Explicit CLI + exact cwd scope, with bounded metadata caching; no default discovery."""
    if not isinstance(scope_cwd, (str, Path)) or not str(scope_cwd).strip():
        raise ValueError("an explicit exact project cwd is required")
    key = (str(Path(cli).resolve()), str(Path(scope_cwd).resolve()))
    with _LOCK:
        if key[0] in _QUARANTINED:
            return {"available": False, "tasks": [], "threads": [], "can_send": False, "can_steer": False,
                    "adapter_mode": "disabled-cleanup-incomplete", "scope_cwd": key[1], "note": NOTE,
                    "observed_at": time.time(), "error": "native CLI probe disabled until app restart: " + _QUARANTINED[key[0]]}
        old = _CACHE.get(key)
        if old and time.monotonic() - old[0] < cache_seconds:
            value = copy.deepcopy(old[1])
            value["cached"] = True
            return value
        value = _read(key[0], key[1], timeout)
        _CACHE[key] = (time.monotonic(), value)
        value = copy.deepcopy(value)
        value["cached"] = False
        return value
