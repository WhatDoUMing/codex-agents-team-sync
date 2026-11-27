"""Windows EXE entry point for the local AAMS dashboard."""

import os
from pathlib import Path
import sys

import sync


APP_NAME = "AAMS"
DEFAULT_PORT = 8766


def user_data_dir():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    return Path.home() / "AppData" / "Local" / APP_NAME


def enabled(value):
    return value.strip().casefold() not in {"0", "false", "no", "off"}


def configure_evidence_roots(data_dir):
    defaults = {
        "AAMS_PROJECT_EVIDENCE_ROOT": "project-evidence",
        "AAMS_DELEGATION_EVIDENCE_ROOT": "delegation-evidence",
    }
    for variable, child in defaults.items():
        configured = os.environ.get(variable)
        root = Path(configured).expanduser() if configured else data_dir / child
        root.mkdir(parents=True, exist_ok=True)
        (root / "field").mkdir(parents=True, exist_ok=True)
        os.environ[variable] = str(root.resolve())


def main():
    data_dir = user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    configure_evidence_roots(data_dir)

    try:
        port = int(os.environ.get("AAMS_PORT", str(DEFAULT_PORT)))
    except ValueError as exc:
        raise SystemExit("AAMS_PORT must be an integer from 0 through 65535.") from exc
    if not 0 <= port <= 65535:
        raise SystemExit("AAMS_PORT must be an integer from 0 through 65535.")

    # The EXE never opts into the legacy CAS control panel. AAMS project
    # dispatch is enabled separately only when an explicit local Hub is set.
    os.environ["AAMS_WINDOWS_RELEASE"] = "1"
    os.environ.pop("CAS_LOOP_PANEL", None)
    os.environ.pop("CAS_LOOP_CONTROL", None)
    args = ["--db", str(data_dir / "sync.sqlite3"), "serve", "--port", str(port)]
    if enabled(os.environ.get("AAMS_OPEN_BROWSER", "1")):
        args.append("--open-browser")
    return sync.main(args)


if __name__ == "__main__":
    sys.exit(main())
