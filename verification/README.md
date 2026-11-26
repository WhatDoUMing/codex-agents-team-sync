# Local verification outputs

Run from the repository root on the supported Windows setup:

~~~powershell
python -B -m unittest -v test_sync test_native_adapter test_codex_bridge
node --check .\ui\app.js
node --check .\browser_qa.js
python -B .\run_browser_qa.py
~~~

The automated tests use isolated temporary databases. Browser QA uses a separate temporary Chrome profile and explicit fixture data, not the user's browser profile or real private chat contents.

The browser runner creates screenshots and a JSON report in this directory. Additional manual API or platform checks may also produce local reports.

All generated outputs are intentionally ignored by Git. Logs, screenshots and reports can contain local paths, account names, session metadata, instructions or other private information. Review and sanitize them before choosing to share them. Do not force-add this directory's generated files.

Native metadata is not cross-process liveness or permission to send. A fixture, acknowledgement or platform message receipt is not evidence that actual project work was executed.

Real owned-worker QA is separately opt-in and consumes account quota. Never use it against an existing work session.
