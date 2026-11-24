#!/usr/bin/env python3
"""Bounded capacity audit and verified artifact preservation; stdlib + gh only.

Never removes Git history, research data, caches or unarchived artifacts.
Only disabled_inactivity workflows may be re-enabled. No trading commands.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

MIB = 1024 ** 2
GIB = 1024 ** 3
STATUS_BRANCH = "ops-health"
STATUS_PATH = "repository-health.json"


def gh(*args, binary=False):
    return subprocess.check_output(["gh", *args], text=not binary, timeout=120,
                                   stderr=subprocess.PIPE)


def api(path, method="GET", payload=None):
    args = ["api", path, "--method", method]
    if payload is None:
        raw = gh(*args)
    else:
        raw = subprocess.check_output(["gh", *args, "--input", "-"],
                                      input=json.dumps(payload), text=True,
                                      timeout=120, stderr=subprocess.PIPE)
    return json.loads(raw) if raw.strip() else {}


def paged(path, key=None):
    result = []
    for page in range(1, 1001):
        data = api(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        items = data[key] if key else data
        if not isinstance(items, list):
            raise ValueError("Unexpected API page; capacity is unknown")
        result.extend(items)
        if len(items) < 100:
            return result
    raise RuntimeError("Pagination bound exceeded; audit is incomplete")


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def repair_inactivity(repo, workflows, apply):
    repaired = []
    for workflow in workflows:
        if workflow["state"] == "disabled_inactivity":
            if apply:
                api(f"repos/{repo}/actions/workflows/{workflow['id']}/enable", "PUT")
            repaired.append(workflow["path"])
    return repaired


def archive_candidates(artifacts, now):
    # Preserve near-expiry evidence without shortening its consumer access window.
    return sorted((a for a in artifacts if not a.get("expired")
                   and utc(a["expires_at"]) <= now + timedelta(days=7)),
                  key=lambda a: (a["expires_at"], a["id"]))


def verify_copy(original, recovered, expected=None):
    digest = sha256(original)
    if expected and expected != "sha256:" + digest:
        raise ValueError("Original artifact digest mismatch")
    if sha256(recovered) != digest:
        raise ValueError("Archive round-trip digest mismatch")
    return digest


def preserve_artifact(repo, artifact, head):
    # Numeric ID buckets cap each release at 500 ZIPs + 500 receipts.
    tag = f"evidence-archive-{artifact['id'] // 500}"
    name = f"artifact-{artifact['id']}.zip"
    receipt_name = f"artifact-{artifact['id']}.json"
    try:
        release = api(f"repos/{repo}/releases/tags/{tag}")
    except subprocess.CalledProcessError as exc:
        if "404" not in (exc.stderr or ""):
            raise
        gh("release", "create", tag, "--repo", repo, "--target", head,
           "--title", tag, "--notes", "Verified original Actions ZIPs and provenance receipts.",
           "--latest=false")
        release = api(f"repos/{repo}/releases/tags/{tag}")
    assets = paged(f"repos/{repo}/releases/{release['id']}/assets")
    names = {a["name"] for a in assets}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        if name in names and receipt_name in names:
            # Receipt is written only AFTER round-trip verification. Validate identity.
            gh("release", "download", tag, "--repo", repo, "--pattern", receipt_name, "--dir", temp)
            receipt = json.loads((root / receipt_name).read_text())
            asset = next(a for a in assets if a["name"] == name)
            if (receipt["artifact"]["id"] != artifact["id"]
                    or receipt["repository"] != repo
                    or not receipt.get("round_trip_verified")
                    or asset["size"] != receipt["bytes"]
                    or (asset.get("digest") and asset["digest"] != "sha256:" + receipt["sha256"])):
                raise ValueError("Existing archive receipt does not match asset")
            return receipt
        original = root / name
        # Stream binary data to disk, never buffer large archives in RAM.
        with original.open("wb") as out:
            subprocess.run(["gh", "api", f"repos/{repo}/actions/artifacts/{artifact['id']}/zip"],
                           stdout=out, stderr=subprocess.PIPE, check=True, timeout=180)
        if original.stat().st_size != artifact["size_in_bytes"]:
            raise ValueError("Artifact download size mismatch")
        if name not in names:
            gh("release", "upload", tag, str(original), "--repo", repo)
        recovered = root / "verify"
        recovered.mkdir()
        gh("release", "download", tag, "--repo", repo, "--pattern", name, "--dir", str(recovered))
        digest = verify_copy(original, recovered / name, artifact.get("digest"))
        receipt = {"repository": repo, "artifact": artifact, "sha256": digest,
                   "bytes": original.stat().st_size, "round_trip_verified": True,
                   "release_tag": tag, "verified_at": datetime.now(timezone.utc).isoformat()}
        (root / receipt_name).write_text(json.dumps(receipt, indent=2) + "\n")
        gh("release", "upload", tag, str(root / receipt_name), "--repo", repo)
        # Original is intentionally retained until GitHub's existing expiry.
        return receipt


def verify_git_blob(path, expected_sha, expected_size):
    size = path.stat().st_size
    digest = hashlib.sha1(f"blob {size}\0".encode())
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    if size != expected_size or digest.hexdigest() != expected_sha:
        raise ValueError('Git blob identity mismatch')


def preserve_git_blob(repo, item, head):
    """Verified backup, with no deletion or change to source branches/readers."""
    sha = item['sha']
    tag = 'source-blob-' + sha
    name = 'source-' + sha + '.bin'
    receipt_name = 'receipt.json'
    try:
        release = api(f'repos/{repo}/releases/tags/{tag}')
    except subprocess.CalledProcessError as exc:
        if '404' not in (exc.stderr or ''):
            raise
        gh('release', 'create', tag, '--repo', repo, '--target', head,
           '--title', 'Source recovery ' + sha[:12], '--notes',
           'Original Git blob, byte-identical backup and provenance receipt. No source deletion.',
           '--latest=false')
        release = api(f'repos/{repo}/releases/tags/{tag}')
    assets = paged(f"repos/{repo}/releases/{release['id']}/assets")
    names = {a['name'] for a in assets}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        if name in names and receipt_name in names:
            gh('release', 'download', tag, '--repo', repo, '--pattern', receipt_name, '--dir', temp)
            receipt = json.loads((root / receipt_name).read_text())
            asset = next(a for a in assets if a['name'] == name)
            if (receipt.get('repository') != repo or receipt.get('git_blob_sha') != sha
                    or receipt.get('bytes') != item['bytes'] or asset['size'] != item['bytes']
                    or not receipt.get('round_trip_verified')
                    or asset.get('digest') != 'sha256:' + receipt.get('sha256', '')):
                raise ValueError('Existing source backup identity mismatch')
            return receipt
        original = root / name
        with original.open('wb') as out:
            subprocess.run(['gh', 'api', f'repos/{repo}/git/blobs/{sha}',
                            '-H', 'Accept: application/vnd.github.raw'], stdout=out,
                           stderr=subprocess.PIPE, check=True, timeout=180)
        verify_git_blob(original, sha, item['bytes'])
        if name not in names:
            gh('release', 'upload', tag, str(original), '--repo', repo)
        recovered = root / 'verify'
        recovered.mkdir()
        gh('release', 'download', tag, '--repo', repo, '--pattern', name, '--dir', str(recovered))
        digest = verify_copy(original, recovered / name)
        receipt = {'repository': repo, 'git_blob_sha': sha, 'bytes': item['bytes'],
                   'source': item, 'sha256': digest, 'release_tag': tag,
                   'round_trip_verified': True, 'verified_at': datetime.now(timezone.utc).isoformat()}
        (root / receipt_name).write_text(json.dumps(receipt, indent=2) + '\n')
        gh('release', 'upload', tag, str(root / receipt_name), '--repo', repo)
        return receipt


def large_file_alerts(items, previous, now):
    old = {(v['branch'], v['path']): v for v in (previous or {}).get('large_files', [])}
    alerts = []
    for item in items:
        label = item['branch'] + ':' + item['path']
        if not item.get('backup_verified'):
            alerts.append(label + ': large source has no verified backup')
        prior = old.get((item['branch'], item['path']))
        baseline = ({'observed_at': previous['observed_at'], 'metrics': {label: prior['bytes']}}
                    if prior else None)
        alerts.extend(threshold_alerts({label: item['bytes']}, {label: 50 * MIB}, baseline, now))
    return alerts


def read_status(repo):
    try:
        item = api(f"repos/{repo}/contents/{STATUS_PATH}?ref={STATUS_BRANCH}")
    except subprocess.CalledProcessError as exc:
        if "404" in (exc.stderr or ""):
            return None
        raise
    return json.loads(base64.b64decode(item["content"]))


def put_file(repo, branch, path, contents, message):
    endpoint = f"repos/{repo}/contents/{path}"
    payload = {"branch": branch, "message": message,
               "content": base64.b64encode(contents.encode()).decode()}
    try:
        old = api(endpoint + "?ref=" + quote(branch, safe=""))
        payload["sha"] = old["sha"]
    except subprocess.CalledProcessError as exc:
        if "404" not in (exc.stderr or ""):
            raise
    return api(endpoint, "PUT", payload)


def save_status(repo, head, report):
    try:
        api(f"repos/{repo}/git/ref/heads/{STATUS_BRANCH}")
    except subprocess.CalledProcessError as exc:
        if "404" not in (exc.stderr or ""):
            raise
        api(f"repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{STATUS_BRANCH}", "sha": head})
    put_file(repo, STATUS_BRANCH, STATUS_PATH, json.dumps(report, indent=2) + "\n",
             "ops: record capacity and schedule health")


def threshold_alerts(metrics, policy, previous, now):
    alerts = []
    for key, limit in policy.items():
        value = metrics.get(key)
        if value is None:
            continue
        if value >= limit:
            alerts.append(f"{key}: {value} >= operational warning budget {limit}")
        if previous and key in previous.get("metrics", {}):
            age = (now - utc(previous["observed_at"])).total_seconds() / 86400
            old = previous["metrics"][key]
            if age >= 0.5 and old is not None and value > old and value < limit:
                days = (limit - value) / ((value - old) / age)
                if days < 14:
                    alerts.append(f"{key}: warning budget projected in {days:.1f} days (linear estimate)")
    return alerts


def audit(repo, apply=False, archive=True):
    now = datetime.now(timezone.utc)
    start = time.monotonic()
    report = {"repository": repo, "observed_at": now.isoformat(), "metrics": {},
              "alerts": [], "errors": [], "repaired": [], "archives": [],
              "billing_quota": "unknown: account billing permission required"}
    metrics, alerts, errors = report["metrics"], report["alerts"], report["errors"]
    metadata = api(f"repos/{repo}")
    branch = metadata["default_branch"]
    head = api(f"repos/{repo}/commits/{quote(branch, safe='')}")
    try:
        previous = read_status(repo)
    except Exception as exc:
        previous = None
        errors.append(f"previous status: {type(exc).__name__}")
    metrics["repository_bytes"] = metadata["size"] * 1024
    for label, operation in (
        ("cache", lambda: api(f"repos/{repo}/actions/cache/usage")),
        ("artifacts", lambda: paged(f"repos/{repo}/actions/artifacts", "artifacts")),
        ("workflows", lambda: paged(f"repos/{repo}/actions/workflows", "workflows")),
    ):
        try:
            value = operation()
            if label == "cache":
                metrics["cache_bytes"] = value["active_caches_size_in_bytes"]
            elif label == "artifacts":
                active = [a for a in value if not a["expired"]]
                metrics["artifact_bytes"] = sum(a["size_in_bytes"] for a in active)
                metrics["artifact_count"] = len(active)
                active_ids = {a['id'] for a in active}
                report['archives'] = [a for a in (previous or {}).get('archives', [])
                                      if a['id'] in active_ids]
                archived_ids = {a['id'] for a in report['archives']}
                candidates = [a for a in archive_candidates(active, now) if a['id'] not in archived_ids]
                completed = 0
                if apply and archive:
                    for artifact in candidates:
                        if time.monotonic() - start > 300 or completed >= 20:
                            break
                        if artifact["size_in_bytes"] > 512 * MIB:
                            errors.append(f"artifact {artifact['id']}: exceeds 512 MiB archive worker limit")
                            continue
                        receipt = preserve_artifact(repo, artifact, head["sha"])
                        report["archives"].append({"id": artifact["id"], "tag": receipt["release_tag"]})
                        completed += 1
                report["archive_candidates"] = len(candidates)
                report["archive_pending"] = len(candidates) - completed
                if apply and archive and report["archive_pending"]:
                    alerts.append(f"Archive backlog: {report['archive_pending']} near-expiry artifacts remain")
            else:
                report["repaired"] = repair_inactivity(repo, value, apply)
                report["workflow_states"] = {w["path"]: w["state"] for w in value}
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}; audit incomplete")
    try:
        releases = paged(f"repos/{repo}/releases")
        metrics['release_count'] = len(releases)
        metrics['release_asset_bytes'] = 0
        for release in releases:
            assets = paged(f"repos/{repo}/releases/{release['id']}/assets")
            metrics['release_asset_bytes'] += sum(a['size'] for a in assets)
            if len(assets) >= 900:
                alerts.append(f"Release {release['tag_name']}: {len(assets)} assets; split before 1000")
            if release['tag_name'] == 'research-state-v1':
                checkpoints = sorted((a for a in assets if a['name'].startswith('state-')
                                      and a['name'].endswith('.sqlite.gz')), key=lambda a:a['name'])
                if checkpoints:
                    latest = checkpoints[-1]
                    metrics['checkpoint_compressed_bytes'] = latest['size']
                    # This application's checkpoint writer creates a single gzip member,
                    # with a strict 768 MiB original size cap, so ISIZE cannot wrap 4 GiB.
                    if latest['size'] <= 512 * MIB:
                        with tempfile.TemporaryDirectory() as temp:
                            gh('release', 'download', 'research-state-v1', '--repo', repo,
                               '--pattern', latest['name'], '--dir', temp)
                            file = Path(temp) / latest['name']
                            if latest.get('digest') and latest['digest'] != 'sha256:' + sha256(file):
                                raise ValueError('Checkpoint digest mismatch')
                            with file.open('rb') as stream:
                                if stream.read(2) != b'\x1f\x8b': raise ValueError('Invalid gzip checkpoint')
                                stream.seek(-4, 2)
                                metrics['checkpoint_uncompressed_bytes'] = int.from_bytes(stream.read(4), 'little')
                    else:
                        alerts.append('Checkpoint compressed size exceeds bounded audit download; inspect ledger')
    except Exception as exc:
        errors.append(f"release capacity: {type(exc).__name__}")
    limits = {"repository_bytes": 700 * MIB, "artifact_bytes": 400 * MIB, "cache_bytes": 8 * GIB,
              'checkpoint_uncompressed_bytes': 576 * MIB}
    report["operational_warning_budgets"] = limits
    alerts.extend(threshold_alerts(metrics, limits, previous, now))
    # Inspect every branch's tree without checking out or downloading data/history.
    try:
        large = []
        for b in paged(f"repos/{repo}/branches"):
            tree = api(f"repos/{repo}/git/trees/{b['commit']['sha']}?recursive=1")
            if tree.get("truncated"):
                errors.append(f"tree {b['name']}: truncated")
            for item in tree.get("tree", []):
                if item.get("size", 0) >= 25 * MIB:
                    large.append({"branch": b["name"], "path": item["path"], "bytes": item["size"], "sha": item["sha"]})
        backups = {}
        for item in large:
            if apply and item['bytes'] <= 100 * MIB:
                if item['sha'] not in backups and len(backups) < 4 and time.monotonic() - start < 600:
                    backups[item['sha']] = preserve_git_blob(repo, item, head['sha'])
                if item['sha'] in backups:
                    item['backup_verified'] = True
                    item['backup_tag'] = backups[item['sha']]['release_tag']
        report['large_files'] = large
        report['large_file_warning_bytes'] = 50 * MIB
        alerts.extend(large_file_alerts(large, previous, now))
    except Exception as exc:
        errors.append(f"branch trees: {type(exc).__name__}")
    last_commit = utc(head["commit"]["committer"]["date"])
    report["default_branch_commit_age_days"] = max(0, (now - last_commit).days)
    if not metadata["private"] and (now - last_commit).days >= 30:
        if apply:
            try:
                put_file(repo, branch, ".github/guardian-heartbeat.txt", now.isoformat() + "\n",
                         "ops: maintain scheduled research activity")
                report["heartbeat_updated"] = True
            except Exception as exc:
                errors.append(f"heartbeat: {type(exc).__name__}")
        else:
            alerts.append("Default branch inactive >=30 days; heartbeat due")
    report["status"] = "incomplete" if errors else "attention" if alerts else "ok"
    if apply:
        try:
            save_status(repo, head["sha"], report)
        except Exception as exc:
            errors.append(f"status persistence: {type(exc).__name__}")
            report["status"] = "incomplete"
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-archive", action="store_true")
    args = parser.parse_args()
    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    report = audit(args.repo, args.apply, not args.no_archive)
    output = json.dumps(report, indent=2)
    print(output)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write("## Repository capacity\n```json\n" + output + "\n```\n")
    for alert in report["alerts"] + report["errors"]:
        print("::warning::" + alert)
    return int(bool(report["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
