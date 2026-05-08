#!/usr/bin/env python3
"""Audit regions.json against actual GitHub state.

For regions with a `gh_query` field, fetch the actual issue count from GitHub
and compare it to the stored `issue_count`. Regions without `gh_query` are
skipped (not yet wired up — add a gh_query field when you want CI to track
drift for that region).

The `gh_query` field accepts either:

  "gh_query": "is:open repo:ChronoAIProject/NyxID label:auth"

or

  "gh_query": {"repo": "ChronoAIProject/NyxID", "label": "auth", "state": "open"}

Usage:
    python3 tools/audit_regions.py            # report mode (exit 0 always)
    python3 tools/audit_regions.py --strict   # exit 1 on drift (CI gate)
    python3 tools/audit_regions.py --markdown # markdown report (for issues)

Requires `gh` CLI to be on PATH and authenticated.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# GitHub search API: 30 req/min authenticated. Sleep 2.1s between calls to stay well under.
SEARCH_THROTTLE_SEC = 2.1


def _is_qualifier_only(query: str) -> str | None:
    """If query has only `qualifier:value` tokens (no free-text search term),
    return the repo to fall back to issue list API. Otherwise return None.
    GitHub search API 422s on qualifier-only queries — list API is the workaround."""
    tokens = query.split()
    free_text = [t for t in tokens if ":" not in t]
    if free_text:
        return None
    repo = next((t.split(":", 1)[1] for t in tokens if t.startswith("repo:")), None)
    return repo


def gh_search_count(query: str) -> int:
    repo = _is_qualifier_only(query)
    if repo:
        cmd = ["gh", "issue", "list", "-R", repo, "--state", "open",
               "--limit", "1000", "--json", "number"]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return len(json.loads(result.stdout))
    cmd = ["gh", "api", "-X", "GET", "search/issues",
           "-f", f"q={query}", "-f", "per_page=1",
           "-q", ".total_count"]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    time.sleep(SEARCH_THROTTLE_SEC)
    return int(result.stdout.strip())


def query_to_string(q) -> str:
    if isinstance(q, str):
        return q
    parts = ["is:issue"]
    if q.get("repo"):
        parts.append(f"repo:{q['repo']}")
    if q.get("label"):
        parts.append(f"label:{q['label']}")
    state = q.get("state", "open")
    if state and state != "all":
        parts.append(f"is:{state}")
    return " ".join(parts)


def audit(regions: dict) -> tuple[list, int]:
    drift = []
    queried = 0
    for rid, r in regions.items():
        gq = r.get("gh_query")
        if not gq:
            continue
        queried += 1
        try:
            actual = gh_search_count(query_to_string(gq))
        except subprocess.CalledProcessError as e:
            drift.append((rid, "?", "ERROR: " + e.stderr.strip()[:80]))
            continue
        expected = int(r.get("issue_count", 0))
        if actual != expected:
            drift.append((rid, expected, actual))
    return drift, queried


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--strict", action="store_true",
                   help="exit 1 if drift detected")
    p.add_argument("--markdown", action="store_true",
                   help="emit GitHub-flavored markdown report")
    args = p.parse_args()

    if not shutil.which("gh"):
        print("ERROR: 'gh' CLI not found on PATH. Install: "
              "https://cli.github.com/", file=sys.stderr)
        sys.exit(2)

    with open(REPO_ROOT / "regions.json") as f:
        doc = json.load(f)
    regions = doc["regions"]
    drift, queried = audit(regions)

    if args.markdown:
        print("# Region Drift Audit")
        print()
        print(f"- Total regions: **{len(regions)}**")
        print(f"- Regions with `gh_query`: **{queried}**")
        print(f"- Regions without `gh_query` (skipped): "
              f"**{len(regions) - queried}**")
        print(f"- Drift detected: **{len(drift)}**")
        print()
        if drift:
            print("## Drift")
            print()
            print("| region | issue_count (json) | actual (GitHub) |")
            print("| --- | --- | --- |")
            for rid, exp, act in drift:
                print(f"| `{rid}` | {exp} | {act} |")
        else:
            print("✅ No drift.")
    else:
        print(f"Total regions: {len(regions)}")
        print(f"Auto-checked (with gh_query): {queried}")
        print(f"Skipped (no gh_query): {len(regions) - queried}")
        print(f"Drift: {len(drift)}")
        for rid, exp, act in drift:
            print(f"  - {rid}: json={exp} actual={act}")

    if drift and args.strict:
        sys.exit(1)


if __name__ == "__main__":
    main()
