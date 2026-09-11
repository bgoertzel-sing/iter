"""Return a read-only status snapshot (git state, running processes, test results)."""

DESCRIPTION = "Return a read-only status snapshot of git state, running processes, and system load."

import json, os, subprocess

def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
        return r.stdout.strip()
    except Exception as e:
        return f"(error: {e})"

git_log = run("git log --oneline -5")
git_status = run("git status --short")
load = run("uptime")

print(json.dumps({
    "git_log": git_log,
    "git_status": git_status,
    "load": load,
}, indent=2))
