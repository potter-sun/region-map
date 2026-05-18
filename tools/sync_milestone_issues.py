#!/usr/bin/env python3
"""Sync GitHub milestone issues into the matching region's description.

For every region with a `milestone_ref` field, fetch all issues in that
milestone (open + closed) and:
  1. Replace the content between the `<!-- AUTO_GH_ISSUES:START -->`
     and `<!-- AUTO_GH_ISSUES:END -->` markers in `desc.{en,zh}` with a
     human-readable issue list (for the side panel).
  2. Write a structured JSON file `ornn-issues.json` at repo root with
     per-region issues + extracted dependencies (for the compound-node
     visualization).

milestone_ref schema:
    "milestone_ref": {
        "repo": "ChronoAIProject/Ornn",
        "milestone": "M0 — Engineering Foundation & Infra"
    }

Dependency extraction from issue body (case-insensitive):
    - "Depends on #N"  / "Depends-on: #N"   → blocked_by[N]
    - "Blocks #N"      / "Blocks: #N"       → blocks[N]
    - "Tracks #N"      / "Tracks: #N"       → tracks[N]
    - "Tracked by #N"  / "Tracked-by: #N"   → tracked_by[N]
    - Checklist items "- [ ] #N" / "- [x] #N" → tracks[N]
    - Plain "#N" mentions are NOT captured (too noisy for the viz)

Usage:
    python3 tools/sync_milestone_issues.py            # update regions.json + ornn-issues.json
    python3 tools/sync_milestone_issues.py --check    # exit 1 if any change needed (CI)
    python3 tools/sync_milestone_issues.py --region <key>  # sync just one region

Requires `gh` CLI authenticated with access to all referenced repos
(including private ones — needs a PAT/GH App in CI for cross-repo or private repo access).
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
REGIONS_FILE = REPO_ROOT / "regions.json"
ISSUES_FILE = REPO_ROOT / "ornn-issues.json"

AUTO_START = "<!-- AUTO_GH_ISSUES:START -->"
AUTO_END = "<!-- AUTO_GH_ISSUES:END -->"
AUTO_BLOCK_RE = re.compile(
    re.escape(AUTO_START) + r".*?" + re.escape(AUTO_END),
    re.DOTALL,
)

# Dependency extraction patterns
DEP_PATTERNS = {
    "blocked_by": [
        re.compile(r"(?im)\bdepends?[\s\-_:]+on\s*#(\d+)"),
        re.compile(r"(?im)\bblocked[\s\-_:]+by\s*#(\d+)"),
    ],
    "blocks": [
        re.compile(r"(?im)\bblocks\s*#(\d+)"),
        re.compile(r"(?im)\bblocks:\s*#(\d+)"),
    ],
    "tracks": [
        re.compile(r"(?im)\btracks\s*#(\d+)"),
        re.compile(r"(?im)\btracks:\s*#(\d+)"),
    ],
    "tracked_by": [
        re.compile(r"(?im)\btracked[\s\-_:]+by\s*#(\d+)"),
    ],
}
# Checklist items in body imply "tracks": the parent issue tracks the child
CHECKLIST_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\][^\n]*?#(\d+)", re.MULTILINE)


def extract_deps(body: str | None) -> dict:
    """Return dict with keys blocks / blocked_by / tracks / tracked_by, values: sorted list of ints."""
    deps = {k: set() for k in DEP_PATTERNS}
    if not body:
        return {k: [] for k in deps}
    for kind, patterns in DEP_PATTERNS.items():
        for pat in patterns:
            for m in pat.finditer(body):
                deps[kind].add(int(m.group(1)))
    # Checklist items → tracks
    for m in CHECKLIST_RE.finditer(body):
        deps["tracks"].add(int(m.group(1)))
    return {k: sorted(v) for k, v in deps.items()}


def fetch_milestone_issues(repo: str, milestone_title: str) -> tuple[list, int | None]:
    """Return (issues, milestone_number). Issues include body for dep extraction.

    Sorted: open first, then closed, both by number desc.
    """
    ms_raw = subprocess.check_output(
        ["gh", "api", f"repos/{repo}/milestones?state=all", "--paginate",
         "-q", ".[] | {number, title, state, description, html_url}"],
        text=True,
    )
    milestones = [json.loads(line) for line in ms_raw.strip().split("\n") if line.strip()]
    matched = next((m for m in milestones if m["title"] == milestone_title), None)
    if not matched:
        print(f"  WARN: milestone '{milestone_title}' not found in {repo}", file=sys.stderr)
        return [], None

    ms_num = matched["number"]
    issues_raw = subprocess.check_output(
        ["gh", "issue", "list", "--repo", repo,
         "--milestone", milestone_title, "--state", "all", "--limit", "500",
         "--json", "number,title,state,url,body,labels"],
        text=True,
    )
    issues = json.loads(issues_raw)
    # Attach extracted deps to each issue
    for i in issues:
        i["deps"] = extract_deps(i.get("body"))
        i["labels"] = [l["name"] for l in (i.get("labels") or [])]
        # Drop body — we don't need it anymore + keeps JSON small
        i.pop("body", None)
    issues.sort(key=lambda i: (i["state"] != "OPEN", -i["number"]))
    return issues, ms_num


def format_block(repo: str, milestone_title: str, ms_num: int | None, issues: list, lang: str) -> str:
    """Render the auto-sync block content (between markers)."""
    if ms_num is None:
        return "_Milestone not found on GitHub — check `milestone_ref.milestone` value._"

    ms_url = f"https://github.com/{repo}/milestone/{ms_num}"
    open_n = sum(1 for i in issues if i["state"] == "OPEN")
    closed_n = sum(1 for i in issues if i["state"] == "CLOSED")

    if lang == "zh":
        header = (
            f"**GitHub issues** — [{milestone_title}]({ms_url}) · "
            f"{open_n} 进行中 / {closed_n} 已关闭"
        )
        open_label = "进行中"
        closed_label = "已关闭"
    else:
        header = (
            f"**GitHub issues** — [{milestone_title}]({ms_url}) · "
            f"{open_n} open / {closed_n} closed"
        )
        open_label = "Open"
        closed_label = "Closed"

    lines = [header, ""]
    open_issues = [i for i in issues if i["state"] == "OPEN"]
    closed_issues = [i for i in issues if i["state"] == "CLOSED"]

    if open_issues:
        lines.append(f"_{open_label}:_")
        for i in open_issues:
            t = i["title"].replace("|", "\\|")
            lines.append(f"- [#{i['number']}]({i['url']}) {t}")
        lines.append("")

    if closed_issues:
        lines.append(f"_{closed_label} (most recent first):_")
        # Cap closed at 25 to keep desc manageable; surface a "+N more" indicator
        cap = 25
        for i in closed_issues[:cap]:
            t = i["title"].replace("|", "\\|")
            lines.append(f"- [#{i['number']}]({i['url']}) ~~{t}~~")
        if len(closed_issues) > cap:
            lines.append(f"- _… and {len(closed_issues) - cap} more closed_")

    return "\n".join(lines)


def replace_block(text: str, new_inner: str) -> str:
    new_block = f"{AUTO_START}\n{new_inner}\n{AUTO_END}"
    if AUTO_BLOCK_RE.search(text):
        return AUTO_BLOCK_RE.sub(new_block, text)
    # No marker block — append one
    return text.rstrip() + "\n\n" + new_block


def sync_region(region: dict, key: str, issues_index: dict) -> bool:
    """Update region's desc in place + populate issues_index entry. Return True if regions.json changed."""
    ref = region.get("milestone_ref")
    if not ref:
        return False
    repo = ref["repo"]
    title = ref["milestone"]
    issues, ms_num = fetch_milestone_issues(repo, title)

    # Populate structured issues index for the compound-node viz
    if ms_num is not None:
        ms_url = f"https://github.com/{repo}/milestone/{ms_num}"
        issues_index[key] = {
            "milestone": {
                "title": title,
                "number": ms_num,
                "url": ms_url,
                "repo": repo,
            },
            "issues": {
                str(i["number"]): {
                    "number": i["number"],
                    "title": i["title"],
                    "state": i["state"],
                    "url": i["url"],
                    "labels": i["labels"],
                    "deps": i["deps"],
                } for i in issues
            },
        }

    changed = False
    for lang in ("en", "zh"):
        desc = region.get("desc", {}).get(lang, "")
        new_inner = format_block(repo, title, ms_num, issues, lang)
        new_desc = replace_block(desc, new_inner)
        if new_desc != desc:
            region.setdefault("desc", {})[lang] = new_desc
            changed = True

    # Update issue_count to actual open count
    if ms_num is not None:
        open_n = sum(1 for i in issues if i["state"] == "OPEN")
        if region.get("issue_count") != open_n:
            region["issue_count"] = open_n
            changed = True

    return changed


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="exit 1 if anything would change")
    p.add_argument("--region", help="sync only this region key")
    args = p.parse_args()

    data = json.loads(REGIONS_FILE.read_text())
    regions = data["regions"]

    targets = [args.region] if args.region else [k for k, r in regions.items() if r.get("milestone_ref")]
    if not targets:
        print("no regions with milestone_ref found")
        return

    # Load existing issues index so single-region runs don't wipe other regions' data
    if ISSUES_FILE.exists():
        try:
            existing_index = json.loads(ISSUES_FILE.read_text())
            issues_index = existing_index.get("regions", {})
        except json.JSONDecodeError:
            issues_index = {}
    else:
        issues_index = {}

    print(f"syncing {len(targets)} regions...")
    any_regions_changed = False
    for key in targets:
        if key not in regions:
            print(f"  SKIP: {key} not in regions.json")
            continue
        changed = sync_region(regions[key], key, issues_index)
        marker = "✓ changed" if changed else "  no-op "
        n_issues = len(issues_index.get(key, {}).get("issues", {}))
        print(f"  {marker} {key}  ({n_issues} issues)")
        any_regions_changed = any_regions_changed or changed

    # Always write issues file (it captures every run's data)
    new_index = {
        "synced_at": datetime.datetime.utcnow().isoformat() + "Z",
        "regions": issues_index,
    }

    # Compare to existing
    issues_file_changed = True
    if ISSUES_FILE.exists():
        try:
            old = json.loads(ISSUES_FILE.read_text())
            # Compare excluding synced_at (which always changes)
            if old.get("regions") == new_index["regions"]:
                issues_file_changed = False
        except json.JSONDecodeError:
            pass

    if args.check:
        if any_regions_changed or issues_file_changed:
            print("\nCHECK mode: outputs are out of date")
            sys.exit(1)
        print("\nno changes")
        return

    if any_regions_changed:
        REGIONS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print("regions.json updated")
    else:
        print("regions.json no-op")

    if issues_file_changed:
        ISSUES_FILE.write_text(json.dumps(new_index, indent=2, ensure_ascii=False) + "\n")
        print("ornn-issues.json updated")
    else:
        print("ornn-issues.json no-op")


if __name__ == "__main__":
    main()
