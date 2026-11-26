"""OPT-IN real Chrome + real Codex worker QA. Consumes account quota."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import sync
import web_server


def main(cli):
    with tempfile.TemporaryDirectory(prefix="owned-browser-qa-") as tmp:
        server = web_server.create_server(Path(tmp)/"sync.sqlite3", port=0, codex_cli=cli)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {**os.environ, "TEAM_SYNC_QA_URL": "http://127.0.0.1:%d/" % server.server_address[1],
               "TEAM_SYNC_QA_TOKEN": server.access_token, "TEAM_SYNC_QA_TEMP": tmp}
        try:
            result = subprocess.run(["node", str(sync.ROOT/"owned_browser_qa.js")], env=env, timeout=300)
            return result.returncode
        finally:
            server.shutdown()
            server.owned.close()
            server.demos.close()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cli", type=Path, required=True, help="trusted official installed Codex executable")
    raise SystemExit(main(p.parse_args().cli))
