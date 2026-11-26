"""Explicit QA against a temporary real Hub and its command workers, not real Codex or Tidecore."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from cas_loop_flow import RULES, connect, start, view
from cas_loop_source import RECEIPT, WorkflowSource
from verify_loop_panel import codex_for_qa, loop_qa_environment, port_for_test

ROOT = Path(__file__).resolve().parent
BASE = hashlib.sha256(b"synthetic original").hexdigest()
CANDIDATE = hashlib.sha256(b"synthetic repaired").hexdigest()


def worker():
    context = json.loads(Path(os.environ["AGENT_AUTOLOOP_CONTEXT"]).read_text(encoding="utf-8"))
    event, = context["events"]
    stage, = [stage for stage, rule in RULES.items() if rule == event["type"]]
    assert event["source"] == "local" and event["subject"] in {"qa-fishgate", "qa-decision"}
    assert event["detail"] == (CANDIDATE if stage == "review" else BASE)
    needs_decision = event["subject"] == "qa-decision"
    assert not needs_decision or stage == "reproduce"
    receipt = dict(outcome=("needs_decision" if needs_decision else
                           {"reproduce": "defect", "repair": "candidate", "review": "verified"}[stage]),
                   input_sha=event["detail"], candidate_sha=CANDIDATE if stage == "repair" else None,
                   evidence_sha=hashlib.sha256((stage + "-fixture").encode()).hexdigest())
    result = {"events": [{"id": event["id"] + ":result", "type": RECEIPT,
             "subject": event["subject"], "detail": json.dumps(receipt)}]}
    if needs_decision:
        result["ask"] = [{"title": "Synthetic decision", "text": "Synthetic decision needed",
                          "options": ["Hold", "Stop"]}]
    print("Synthetic checkpoint complete.\n```agent-autoloop\n" + json.dumps(result) + "\n```")


def run(root):
    node = shutil.which("node")
    assert node
    entry = Path.home()/".agent-autoloop"/"app"/"bin"/"agent-autoloop.ts"
    assert entry.is_file()
    env = loop_qa_environment(root, codex_for_qa())
    port = port_for_test()
    assert port not in {3350, 8765, 8766}
    cli = [node, "--disable-warning=ExperimentalWarning", str(entry)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def execute(*args):
        result = subprocess.run(cli+list(args), env=env, cwd=root, timeout=20,
                                capture_output=True, creationflags=flags)
        if result.returncode:
            raise RuntimeError(f"{args[0]} failed: " + result.stderr.decode("utf-8", "replace"))

    execute("init", "--agent", "codex", "--port", str(port), "--no-service")
    config_path = root/"config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["url"] == f"http://127.0.0.1:{port}" and config["hub"]["listen"] == f"127.0.0.1:{port}"
    config.pop("channels", None)
    config["worker"]["capacity"] = 1
    config_path.write_text(json.dumps(config), encoding="utf-8")
    agents = {}
    rules = []
    for stage, name in RULES.items():
        agents[stage] = {"name": stage, "use": "command", "worker": config["worker"]["name"],
                         "command": [sys.executable, str(Path(__file__).resolve()), "--worker"], "maxActive": 1}
        rules.append({"name": name, "to": stage, "cwd": root.as_posix(),
                      "when": {"source": "local", "types": [name]},
                      "emits": {"source": "local", "types": [RECEIPT]}, "tell": "never"})
    (root/"rules.json").write_text(json.dumps({"sources": {"local": {"name": "CAS QA"}},
          "agents": agents, "rules": rules, "channels": {}, "elsewhere": []}), encoding="utf-8")
    execute("check")
    with (root/"hub.log").open("wb") as log, subprocess.Popen(
            cli+["up"], env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=flags) as hub:
        db = None
        try:
            from urllib.request import build_opener, ProxyHandler
            opener = build_opener(ProxyHandler({}))
            for _ in range(60):
                if hub.poll() is not None:
                    raise RuntimeError("isolated Loop exited before ready")
                try:
                    with opener.open(config["url"]+"/healthz", timeout=0.5) as res:
                        if res.status == 200:
                            break
                except OSError:
                    time.sleep(.25)
            else:
                raise RuntimeError("isolated Hub not ready")
            db = connect(root/"workflow.db")
            flow = "qa-fishgate"
            start(db, flow, BASE)
            results = []
            for stage in RULES:
                row = view(db, flow)[-1]
                assert row["stage"] == stage and row["state"] == "ready"
                source = WorkflowSource(db, port, config["source"]["key"])
                eid = row["event_id"]
                number = source.dispatch(eid)
                # Close/reopen before receipt: the source event mapping must survive restart.
                db.close()
                db = connect(root/"workflow.db")
                source = WorkflowSource(db, port, config["source"]["key"])
                for _ in range(140):
                    state, run_id = source.reconcile(eid)
                    if state == "done":
                        break
                    time.sleep(.25)
                else:
                    raise RuntimeError(f"{stage} has no matching terminal receipt; last={state}")
                assert source.reconcile(eid) == ("done", run_id)
                results.append([stage, number, run_id])
            assert [r["state"] for r in view(db, flow)] == ["defect", "candidate", "verified"]
            assert len({item[2] for item in results}) == 3

            from urllib.request import Request
            def owner(route, write=False):
                request = Request(config["url"] + route, data=b"{}" if write else None, headers={
                    "Authorization": "Bearer " + config["owner"], "Content-Type": "application/json",
                })
                with opener.open(request, timeout=4) as res:
                    return json.load(res)

            route = "/v1/rules/" + RULES["reproduce"]
            assert owner(route + "/pause", True)["changed"]
            paused = start(db, "qa-paused", BASE)
            assert source.dispatch(paused) > 0
            assert source.reconcile(paused) == ("rule_paused", None)
            assert owner(route + "/resume", True)["changed"]
            assert source.reconcile(paused) == ("rule_paused", None)
            assert view(db, "qa-paused")[0]["run_id"] is None

            asked = start(db, "qa-decision", BASE)
            source.dispatch(asked)
            for _ in range(140):
                state, run_id = source.reconcile(asked)
                if state == "done":
                    break
                time.sleep(.25)
            else:
                raise RuntimeError("decision worker did not finish")
            assert [row["state"] for row in view(db, "qa-decision")] == ["needs_decision"]
            cards = owner("/v1/state")["cards"]
            assert any(card["state"] == "open" and card["title"] == "Synthetic decision" for card in cards)
            print(json.dumps({"isolated_hub": True, "command_workers": True, "codex": False,
                              "stages": results, "final": "verified", "restart_reconciliation": True,
                              "paused_not_assigned_after_resume": True, "decision_stops_handoff": True}))
        finally:
            if db is not None:
                db.close()
            if hub.poll() is None:
                subprocess.run([str(Path(os.environ["SystemRoot"])/"System32"/"taskkill.exe"),
                                "/PID", str(hub.pid), "/T", "/F"], timeout=10, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags)
                hub.wait(timeout=10)


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        worker()
    else:
        with tempfile.TemporaryDirectory(prefix="cas-workflow-qa-", dir=ROOT) as directory:
            run(Path(directory))
