"""Launch the loopback dashboard with the installed official Codex app, explicitly."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import sync


# Query installed Windows packages only. No PATH search, download, installation,
# security setting changes, or browser-supplied executable.
_PACKAGE_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$items = @(
    foreach ($package in @(Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction Stop)) {
        [PSCustomObject]@{
            family = [string]$package.PackageFamilyName
            publisher = [string]$package.Publisher
            signature = [string]$package.SignatureKind
            status = [string]$package.Status
            location = [string]$package.InstallLocation
        }
    }
)
[Console]::Out.Write((ConvertTo-Json -InputObject $items -Compress))
"""
_OFFICIAL_IDENTITY = {
    "family": "OpenAI.Codex_2p2nqsd0c76g0",
    "publisher": "CN=50BDFD77-8903-4850-9FFE-6E8522F64D5B",
    "signature": "Store",
    "status": "Ok",
}


def find_installed_codex():
    """Return the verified installed app's CLI, or None. Never use a fallback."""
    if sys.platform != "win32" or not os.environ.get("SystemRoot"):
        return None
    powershell = (Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" /
                  "v1.0" / "powershell.exe")
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _PACKAGE_QUERY],
            capture_output=True, encoding="utf-8", timeout=15, check=False,
        )
        if result.returncode:
            return None
        packages = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return None
    if not isinstance(packages, list):
        return None
    for package in packages:
        if not isinstance(package, dict) or any(
            not isinstance(package.get(key), str) or package[key].casefold() != expected.casefold()
            for key, expected in _OFFICIAL_IDENTITY.items()
        ):
            continue
        location = package.get("location")
        if not isinstance(location, str) or not location:
            continue
        root = Path(location)
        if not root.is_absolute():
            continue
        candidate = root / "app" / "resources" / "codex.exe"
        if candidate.is_file():
            return candidate
    return None


def _port(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=_port, default=8765, help="loopback port (default: 8765)")
    args = parser.parse_args(argv)
    cli = find_installed_codex()
    if cli is None:
        print("Official installed Codex app CLI not found or not accessible. "
              "No service was started and nothing was installed.", file=sys.stderr)
        return 1
    print("Real Codex support enabled for NEW owned sessions only; no worker starts automatically.")
    print("Real model requests may use account quota. Keep the session key private.")
    return sync.main(["serve", "--host", "127.0.0.1", "--port", str(args.port),
                      "--codex-cli", str(cli)])


if __name__ == "__main__":
    raise SystemExit(main())
