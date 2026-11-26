"""Official stdio transport, scoped exclusively to one newly created owned thread."""
import json
from pathlib import Path
import queue
import subprocess
import threading
import time

MODEL = "gpt-6.1-sol"
EFFORT = "max"
DISABLED = ("apps", "plugins", "multi_agent", "browser_use", "browser_use_external",
            "browser_use_full_cdp_access", "computer_use", "image_generation", "view_image",
            "memories", "hooks", "sleep_tool", "shell_tool", "unified_exec",
            "skill_search", "skill_mcp_dependency_install", "tool_suggest", "daemon_auto_start")
WAIT_TOOL = {"type": "function", "name": "bridge_wait",
    "description": "Wait up to 10 seconds for operator input. No files, commands, or external services.",
    "inputSchema": {"type": "object", "properties": {"seconds": {"type": "integer", "minimum": 1, "maximum": 10}},
                    "required": ["seconds"], "additionalProperties": False}}


class DeliveryUnknown(TimeoutError):
    pass


class CleanupUnconfirmed(OSError):
    pass


class OwnedRPC:
    def __init__(self, cli, cwd):
        self.cli, self.cwd = Path(cli).resolve(), Path(cwd).resolve()
        if not self.cli.is_file():
            raise ValueError("trusted installed official CLI is missing")
        self.events = queue.Queue(maxsize=2048)
        self.pending = {}
        self.lock, self.write_lock = threading.Lock(), threading.Lock()
        self.serial, self.own_thread = 0, None
        self.start_attempted = False
        self.fault = None
        self.closing = False
        self.unknown_fault = False
        self.write_queue = queue.Queue(maxsize=64)
        self.write_busy = threading.Event()
        self.write_deadline = None
        args = [str(self.cli), "app-server", "--listen", "stdio://", "-c", "mcp_servers={}",
                "-c", 'web_search="disabled"']
        for feature in DISABLED:
            args += ["--disable", feature]
        self.process = subprocess.Popen(args, cwd=str(self.cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.readers = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._stderr, daemon=True)]
        self.writer = threading.Thread(target=self._writer, daemon=True)
        self.monitor = threading.Thread(target=self._watch_writer, daemon=True)
        for reader in self.readers:
            reader.start()
        self.writer.start()
        self.monitor.start()

    def _fail(self, message):
        self.fault = str(message)[:500]
        with self.lock:
            for pending in self.pending.values():
                if "response" not in pending:
                    pending["error"] = self.fault
                    pending["event"].set()

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line) > 2_000_000:
                    self._fail("oversized RPC frame")
                    break
                try:
                    value = json.loads(line)
                except (ValueError, RecursionError):
                    self._fail("invalid RPC frame")
                    break
                if not isinstance(value, dict):
                    continue
                if "id" in value and "method" not in value:
                    with self.lock:
                        target = self.pending.get(value["id"])
                        if target:
                            target["response"] = value
                            target["event"].set()
                else:
                    try:
                        self.events.put(value, timeout=1)
                    except queue.Full:
                        self._fail("owned event buffer overflow")
                        break
        except (OSError, ValueError) as exc:
            self._fail(exc)
        finally:
            self._fail("owned App Server stream ended")

    def _stderr(self):
        try:
            for _ in self.process.stderr:
                pass  # Never persist account details, tokens, prompts, or raw tracing.
        except (OSError, ValueError):
            pass

    def _abort(self, message):
        self.unknown_fault = True
        self._fail(message)
        if self.process.poll() is None:
            try:
                self.process.terminate()  # Only this exact owned child, never a desktop daemon.
            except OSError:
                pass

    def _write(self, value, deadline=None):
        deadline = time.monotonic() + 12 if deadline is None else deadline
        if self.closing or self.fault or self.process.poll() is not None:
            raise ConnectionError(self.fault or "owned transport is closing")
        frame = json.dumps(value, ensure_ascii=False) + "\n"
        try:
            self.write_queue.put((frame, deadline), timeout=max(0, deadline - time.monotonic()))
        except queue.Full:
            self._abort("owned write queue deadline; outcome unknown")
            raise DeliveryUnknown("write queue deadline; no automatic retry") from None

    def _writer(self):
        try:
            while True:
                item = self.write_queue.get()
                if item is None:
                    break
                if self.closing or self.fault:
                    continue
                frame, self.write_deadline = item
                if time.monotonic() >= self.write_deadline:
                    self._abort("owned write deadline; outcome unknown")
                    break
                self.write_busy.set()
                try:
                    self.process.stdin.write(frame)
                    self.process.stdin.flush()
                finally:
                    self.write_busy.clear()
        except (OSError, ValueError) as exc:
            if not self.closing:
                self._abort("owned input write failed: " + str(exc)[:200])
        finally:
            try:
                self.process.stdin.close()  # Only the writer thread touches this TextIO lock.
            except (OSError, ValueError):
                pass

    def _watch_writer(self):
        while not self.closing and self.process.poll() is None:
            if self.write_busy.is_set() and self.write_deadline is not None and time.monotonic() >= self.write_deadline:
                self._abort("owned pipe write deadline; outcome unknown")
                return
            time.sleep(.05)

    def request(self, method, params, timeout=12):
        allowed = {"initialize", "thread/start", "turn/start", "turn/steer", "turn/interrupt"}
        if method not in allowed:
            raise ValueError("RPC is not exposed by owned bridge")
        if method == "thread/start":
            if self.start_attempted:
                raise ValueError("never retry uncertain thread creation")
            self.start_attempted = True
            if params.get("environments") != [] or not params.get("ephemeral") or params.get("model") != MODEL:
                raise ValueError("owned worker must use a new ephemeral, environment-disabled fixed-model thread")
        elif method.startswith("turn/"):
            if not self.own_thread or params.get("threadId") != self.own_thread:
                raise ValueError("only this process's newly created thread is controllable")
            if method == "turn/start" and (params.get("environments") != [] or params.get("model") != MODEL):
                raise ValueError("environment/model override is not allowed")
        deadline = time.monotonic() + timeout
        with self.lock:
            self.serial += 1
            request_id = self.serial
            pending = {"event": threading.Event()}
            self.pending[request_id] = pending
        try:
            self._write({"id": request_id, "method": method, "params": params}, deadline)
            if not pending["event"].wait(max(0, deadline - time.monotonic())):
                self._abort("API total deadline; outcome unknown")
                raise DeliveryUnknown("API outcome unknown; no automatic retry")
            if pending.get("error"):
                if self.unknown_fault:
                    raise DeliveryUnknown(pending["error"])
                raise ConnectionError(pending["error"])
            response = pending["response"]
            if "error" in response:
                error = response["error"]
                raise ValueError(str(error.get("message", error))[:800] if isinstance(error, dict) else str(error)[:800])
            result = response.get("result")
            if not isinstance(result, dict):
                raise DeliveryUnknown("malformed API result; outcome unknown")
            if method == "turn/start":
                turn = result.get("turn")
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
                    raise DeliveryUnknown("missing accepted turn ID; outcome unknown")
            if method == "turn/steer" and result.get("turnId") != params.get("expectedTurnId"):
                raise DeliveryUnknown("steer returned a different turn; outcome unknown")
            if method == "thread/start":
                self.own_thread = result["thread"]["id"]
                if result.get("model") not in (None, MODEL):
                    raise ValueError("server returned a different model")
                if result["thread"].get("environments") != []:
                    raise ValueError("server did not confirm environment access is disabled")
            return result
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def initialize(self):
        self.request("initialize", {"clientInfo": {"name": "local_team_sync_owned_worker",
            "title": "Local Team Sync owned conversation worker", "version": "0.3.0"},
            "capabilities": {"experimentalApi": True}})
        self._write({"method": "initialized", "params": {}})

    def respond(self, request_id, result=None, error=None):
        self._write({"id": request_id, **({"error": error} if error else {"result": result})})

    def close(self):
        self.closing = True
        while True:
            try:
                self.write_queue.get_nowait()
            except queue.Empty:
                break
        self.write_queue.put_nowait(None)
        self.writer.join(timeout=.3)
        if self.writer.is_alive() and self.process.poll() is None:
            self.process.terminate()  # Unblock our own stuck pipe, never wait on its TextIO lock.
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.writer.join(timeout=.5)
        self.monitor.join(timeout=.2)
        for reader in self.readers:
            reader.join(timeout=.5)
        incomplete = self.writer.is_alive() or self.monitor.is_alive()
        for reader, stream in zip(self.readers, (self.process.stdout, self.process.stderr)):
            if reader.is_alive():
                incomplete = True
            else:
                stream.close()
        if incomplete:
            raise CleanupUnconfirmed("owned helper pipe still open; stopping is unconfirmed")
