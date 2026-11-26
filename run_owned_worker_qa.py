"""OPT-IN real model QA through authenticated localhost HTTP. Consumes account quota."""
import argparse
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import urllib.request
import sync
import web_server


def run(cli):
    summary = {"existing_sessions_touched": False, "real_worker": True}
    with tempfile.TemporaryDirectory(prefix="owned-http-qa-") as tmp:
        db = Path(tmp) / "sync.sqlite3"
        server = web_server.create_server(db, port=0, codex_cli=cli)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = "http://127.0.0.1:%d" % server.server_address[1]
        def request(path, body=None):
            headers = {"Authorization": "Bearer " + server.access_token}
            data = None
            if body is not None:
                headers.update({"Origin": origin, "Content-Type": "application/json"})
                data = json.dumps(body).encode()
            req = urllib.request.Request(origin + path, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.load(response)
        def wait(predicate, label, seconds=100):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                snapshot = request("/api/snapshot")
                if predicate(snapshot): return snapshot
                time.sleep(.1)
            raise TimeoutError(label)
        def command(worker, text, mode="queue", turn=None):
            key = "qa-" + secrets.token_hex(12)
            result = request("/api/owned/commands", {"project": "owned-qa", "worker": worker,
                "text": text, "command_id": key, "mode": mode, "expected_turn": turn})
            assert result["state"] == "queued"
            return key
        try:
            created = request("/api/owned/start", {"project": "owned-qa", "request_id": "real-http-proof"})
            worker = created["id"]
            wait(lambda s: s["workers"] and s["workers"][0].get("native", {}).get("state") == "idle", "worker ready", 35)
            first = command(worker, "请调用bridge_wait等待8秒；期间操作者会追加随机校验词。等待结束后只回复追加消息指定的随机词。不要调用其他工具、读写文件或外部服务。")
            snapshot = wait(lambda s: s["workers"][0]["native"].get("waiting_for_operator"), "real bounded tool")
            active = snapshot["workers"][0]["native"]["active_turn"]
            nonce = "HTTP_STEER_" + secrets.token_hex(16)
            second = command(worker, "追加要求：最终只逐字回复 " + nonce, "steer", active)
            snapshot = wait(lambda s: all(next(c for c in s["commands"] if c["id"] == k)["state"] == "completed" for k in (first, second)), "two mapped command results")
            results = [next(c for c in snapshot["commands"] if c["id"] == k) for k in (first, second)]
            assert all(c["result"].strip() == nonce for c in results)
            assert len({c["native_delivery"]["turn_id"] for c in results}) == 1
            summary["same_turn_random_steer_verified"] = True
            nonce2 = "HTTP_SECOND_" + secrets.token_hex(16)
            third = command(worker, "新一轮任务，只回复 " + nonce2 + "，不调用工具。")
            snapshot = wait(lambda s: next(c for c in s["commands"] if c["id"] == third)["state"] == "completed", "next idle turn")
            assert next(c for c in snapshot["commands"] if c["id"] == third)["result"].strip() == nonce2
            summary["second_turn_verified"] = True
            fourth = command(worker, "调用bridge_wait等待8秒，再回复WAIT_FINISHED。")
            wait(lambda s: s["workers"][0]["native"].get("waiting_for_operator"), "active stop target")
            response = request("/api/owned/stop", {"project": "owned-qa", "worker": worker})
            assert response["stop_requested"] is True
            snapshot = wait(lambda s: s["workers"][0]["native"]["state"] in ("stopped", "failed"), "stop completion", 25)
            native = snapshot["workers"][0]["native"]
            assert native["state"] == "stopped", native
            assert native["cleanup_confirmed"] == 1
            last = next(c for c in snapshot["commands"] if c["id"] == fourth)
            assert last["state"] == "rejected" and last["native_delivery"]["phase"] == "turn_interrupted", last
            actor = server.owned.actor("owned-qa", worker)
            assert actor.client.process.poll() is not None and not any(t.is_alive() for t in actor.client.readers)
            summary["active_interrupt_and_owned_shutdown_verified"] = True
        finally:
            server.shutdown()
            server.owned.close()
            server.demos.close()
            server.server_close()
            thread.join(5)
    summary["passed"] = True
    summary["observed_at"] = time.time()
    (sync.ROOT / "verification" / "owned-http-proof.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cli", type=Path, required=True, help="trusted official installed Codex executable")
    args = p.parse_args()
    run(args.cli)
