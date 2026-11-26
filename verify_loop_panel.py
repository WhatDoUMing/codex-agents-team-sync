"""Explicit Windows QA: fresh isolated Loop, real Codex, one headless UI click.
Never use a real browser profile or the production Loop home. No system service.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import web_server
from loop_probe import Probe, RULE
from start_owned_codex import find_installed_codex

ROOT = Path(__file__).resolve().parent


def codex_for_qa():
    configured_path = os.environ.get("TEAM_SYNC_CODEX_CLI")
    cli = Path(configured_path).expanduser() if configured_path else find_installed_codex()
    if cli is None or not cli.is_absolute() or not cli.is_file():
        raise RuntimeError(
            "Set TEAM_SYNC_CODEX_CLI to the absolute path of a trusted Codex executable "
            "or install the official Windows Codex app."
        )
    return cli


def loop_qa_environment(root, codex_cli):
    env = {
        name: value for name, value in os.environ.items()
        if not name.upper().startswith(("AGENT_AUTOLOOP_", "CODEX_"))
    }
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    env["CODEX_HOME"] = str(codex_home.expanduser().resolve())
    env["AGENT_AUTOLOOP_HOME"] = str(root)
    env["PATH"] = os.pathsep.join(filter(None, (str(codex_cli.parent), env.get("PATH"))))
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def port_for_test():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_qa(root):
    codex_cli = codex_for_qa()
    node = shutil.which("node")
    chrome = Path(os.environ.get("TEAM_SYNC_CHROME", "C:/Program Files/Google/Chrome/Application/chrome.exe"))
    if not node or not chrome.is_file():
        raise RuntimeError("Node or Chrome is missing")
    app = Path.home()/".agent-autoloop"/"app"
    entry = app/"bin"/"agent-autoloop.ts"
    if not entry.is_file():
        raise RuntimeError("Loop install missing")
    env = loop_qa_environment(root, codex_cli)
    proc_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    command = [node, "--disable-warning=ExperimentalWarning", str(entry)]
    port = port_for_test()
    subprocess.run(command+["init","--agent","codex","--port",str(port),"--no-service"],
                   env=env, cwd=root, check=True, timeout=15, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, creationflags=proc_flags)
    config_file = root/"config.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    endpoint = "http://127.0.0.1:"+str(port)
    if config["url"] != endpoint or config["hub"]["listen"] != endpoint.removeprefix("http://"):
        raise RuntimeError("isolated Loop endpoint mismatch")
    config.pop("channels",None)
    config["worker"]["capacity"] = 1
    config_file.write_text(json.dumps(config),encoding="utf-8")
    work = root/"fixture"
    work.mkdir()
    marker = "CAS-PROBE-"+secrets.token_hex(16)
    (work/"marker.txt").write_text(marker,encoding="utf-8")
    rules = {"sources":{"local":{"name":"isolated"}},
             "agents":{"codex":{"name":"Codex","worker":config["worker"]["name"],"use":"codex",
                                "model":"gpt-6-astra","access":"read","program":codex_cli.as_posix(),"timeout":90,"maxActive":1}},
             "channels":{},"elsewhere":[],"rules":[
                {"name":RULE,"when":{"source":"local","types":["cas.test.manual-only"]},
                 "to":"codex","cwd":work.as_posix(),
                 "prompt":"Read marker.txt in the current working directory using a real read-only shell tool call. Return only its exact content, as the first and only line. Do not edit anything."}]}
    (root/"rules.json").write_text(json.dumps(rules),encoding="utf-8")
    subprocess.run(command+["check"],env=env,cwd=root,check=True,timeout=15,
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=proc_flags)
    probe = Probe(port,config["owner"],marker)
    with (root/"hub.log").open("wb") as log:
        hub = subprocess.Popen(command+["up"],env=env,cwd=root,stdout=log,
                               stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,creationflags=proc_flags)
        server = None
        try:
            for _ in range(40):
                if hub.poll() is not None:
                    raise RuntimeError("isolated Hub exited")
                if probe.status()["state"]=="ready":
                    break
                time.sleep(.25)
            else:
                raise RuntimeError("isolated Hub not ready; no job dispatched")
            server = web_server.create_server(root/"cas.db",port=0,loop_probe=probe)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            browser_env={**os.environ,"TEAM_SYNC_QA_URL":"http://127.0.0.1:"+str(server.server_port)+"/",
                         "TEAM_SYNC_QA_TOKEN":server.access_token,"TEAM_SYNC_QA_TEMP":str(root),
                         "TEAM_SYNC_CHROME":str(chrome)}
            result=subprocess.run([node,str(ROOT/"loop_probe_browser_qa.js")],env=browser_env,cwd=root,
                                  timeout=135,creationflags=proc_flags)
            state=probe.status()
            run=state.get("run")
            print(json.dumps({"run":run,"state":state.get("state"),"receipt_matches":state.get("receipt_matches"),
                              "browser_exit":result.returncode}),flush=True)
            if not run or result.returncode or not state.get("receipt_matches"):
                return 1
            read=False
            for file in (root/"worker"/"runs").glob(f"{run}-*/stdout-*.log"):
                for line in file.read_text(encoding="utf-8").splitlines():
                    try: event=json.loads(line)
                    except ValueError: continue
                    item=event.get("item",{})
                    if (item.get("type")=="command_execution" and item.get("status")=="completed"
                            and item.get("exit_code")==0 and "marker.txt" in item.get("command","")
                            and item.get("aggregated_output","").strip()==marker):
                        read=True
            print(json.dumps({"run":run,"actual_marker_read":read,"same_run":run==1}),flush=True)
            return 0 if read and run==1 else 1
        finally:
            if server:
                server.shutdown()
                thread.join()
                server.server_close()
            if hub.poll() is None:
                # Exact child spawned for this isolated run, including any test worker descendants.
                subprocess.run([str(Path(os.environ["SystemRoot"])/"System32"/"taskkill.exe"),
                                "/PID",str(hub.pid),"/T","/F"],timeout=10,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=proc_flags)
                hub.wait(timeout=10)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cas-loop-probe-",dir=ROOT) as location:
        raise SystemExit(run_qa(Path(location)))
