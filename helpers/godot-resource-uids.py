#!/usr/bin/env python3
"""Reject duplicate Godot resource identities in a caller-selected prospective Git worktree."""
import re
from pathlib import Path
import subprocess
import sys


def resource_uid(path):
    text = path.read_text()
    if path.suffix == ".uid":
        return text.strip()
    if path.suffix == ".import":
        found = re.search(r'^uid="(uid://[^\"]+)"', text, re.MULTILINE)
    else:
        # Only the resource header owns its UID; ext_resources reference others.
        found = re.search(r'uid="(uid://[^\"]+)"', text.splitlines()[0])
    return found.group(1) if found else None


def check(root):
    paths = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
         "--", "*.import", "*.uid", "*.tscn", "*.tres"], cwd=root).decode().split("\0")
    owners = {}
    for name in sorted(set(filter(None, paths))):
        path = root / name
        if not path.is_file():
            continue
        uid = resource_uid(path)
        if uid:
            owners.setdefault(uid, []).append(name)
    duplicates = {uid: names for uid, names in owners.items() if len(names) > 1}
    for uid, names in duplicates.items():
        print(f"Duplicate {uid}: {'; '.join(names)}")
    if not duplicates:
        print("PASS: resource UIDs are unique")
    return bool(duplicates)


if __name__ == "__main__":
    raise SystemExit(check(Path(sys.argv[1])))
