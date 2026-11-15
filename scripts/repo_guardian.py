#!/usr/bin/env python3
"""Read-only capacity audit; only re-enables workflows disabled by inactivity."""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

repo = os.environ["GITHUB_REPOSITORY"]
token = os.environ["GH_TOKEN"]
base = "https://api.github.com"
headers = {
    "Authorization": "Bearer " + token,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
def api(method, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=25) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}

warnings = []
notes = []
metadata = api("GET", "/repos/" + repo)
size_kib = metadata["size"]
size_mib = size_kib / 1024
print(f"Repository: {repo}, reported size: {size_mib:.1f} MiB")
if size_mib >= 900:
    warnings.append(f"CRITICAL: GitHub-reported repository size {size_mib:.1f} MiB (>=900 MiB)")
elif size_mib >= 700:
    warnings.append(f"WARNING: GitHub-reported repository size {size_mib:.1f} MiB (>=700 MiB)")

# Current-tree scan only: does not measure historical Git objects or LFS storage.
tree = subprocess.check_output(["git", "ls-tree", "-r", "-l", "HEAD"], text=True)
large = []
for line in tree.splitlines():
    parts = line.split(None, 4)
    if len(parts) != 5 or not parts[3].isdigit():
        continue
    byte_size = int(parts[3])
    if byte_size >= 25 * 1024 * 1024:
        large.append((byte_size, parts[4]))
large.sort(reverse=True)
for byte_size, path in large[:10]:
    warnings.append(f"Large tracked file: {path} ({byte_size / 1048576:.1f} MiB)")
if len(large) > 10:
    warnings.append(f"...and {len(large) - 10} more tracked files >=25 MiB")

# Only repair the GitHub state explicitly attributed to 60-day inactivity.
page = 1
while True:
    response = api("GET", f"/repos/{repo}/actions/workflows?per_page=100&page={page}")
    items = response.get("workflows", [])
    for workflow in items:
        if workflow.get("state") != "disabled_inactivity":
            continue
        name = workflow.get("path", workflow.get("name", "unknown"))
        try:
            api("PUT", f"/repos/{repo}/actions/workflows/{workflow['id']}/enable")
            notes.append(f"Re-enabled inactivity-disabled workflow: {name}")
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            warnings.append(f"Failed to re-enable inactivity-disabled workflow {name}: {error}")
    if len(items) < 100:
        break
    page += 1

for warning in warnings:
    print("ALERT:", warning)
for note in notes:
    print("RECOVERED:", note)
if not warnings and not notes:
    print("No configured capacity/workflow issues detected.")
    sys.exit(0)

title = "[Repo Guardian] Capacity or scheduled-workflow event"
body = (
    "Automated non-destructive repository check.\n\n"
    + f"Repository: {repo}\n\n"
    + "Warnings:\n" + ("\n".join("- " + w for w in warnings) if warnings else "- None") + "\n\n"
    + "Recovered workflows:\n" + ("\n".join("- " + n for n in notes) if notes else "- None") + "\n\n"
    + "Limits: repository size is GitHub metadata; current-tree file scan does not cover "
    "historical Git objects, artifact storage, Actions caches, or Git LFS. "
    "This workflow cannot self-recover if it too becomes disabled.\n"
)
issues = api("GET", f"/repos/{repo}/issues?state=open&per_page=100")
existing = next((issue for issue in issues if issue.get("title") == title and "pull_request" not in issue), None)
if existing:
    api("POST", f"/repos/{repo}/issues/{existing['number']}/comments", {"body": body})
    print("Updated existing guardian issue", existing["number"])
else:
    issue = api("POST", f"/repos/{repo}/issues", {"title": title, "body": body})
    print("Created guardian issue", issue["number"])
