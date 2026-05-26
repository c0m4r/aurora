#!.venv/bin/python
"""Check base deps in requirements.txt for newer versions on PyPI.

Releases younger than MIN_AGE_DAYS are ignored so freshly published versions
have time to be flagged before being adopted.

Usage:
    python pip_check.py [requirements_file]

Default:
    requirements_file = requirements.txt
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from packaging.version import InvalidVersion, Version

MIN_AGE_DAYS = 7


def parse_requirements(filepath: str) -> list[tuple[str, str]]:
    """Parse a requirements file with `name[extras]==version` lines."""
    packages: list[tuple[str, str]] = []
    with open(filepath) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            m = re.match(r"^([a-zA-Z0-9._-]+)(?:\[[^\]]+\])?==(.+)$", line)
            if m:
                packages.append((m.group(1), m.group(2)))
            else:
                print(f"  [warn] could not parse: {line}", file=sys.stderr)
    return packages


def fetch_releases(name: str) -> dict[str, str] | None:
    """Return {version: earliest_upload_time_iso} for non-yanked releases."""
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pip_check/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  [error] HTTP {e.code} fetching {name}", file=sys.stderr)
        return None
    except (urllib.error.URLError, OSError) as e:
        print(f"  [error] network error fetching {name}: {e}", file=sys.stderr)
        return None

    out: dict[str, str] = {}
    for version, files in data.get("releases", {}).items():
        times = [f["upload_time_iso_8601"] for f in files if not f.get("yanked")]
        if times:
            out[version] = min(times)
    return out


def parse_iso(iso_str: str) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware datetime."""
    iso_str = iso_str.replace("Z", "+00:00")
    return datetime.fromisoformat(iso_str)


def find_latest_eligible(
    releases: dict[str, str], min_age: timedelta
) -> tuple[Version, datetime] | None:
    """Return (version, upload_time) for the highest non-prerelease older than min_age."""
    cutoff = datetime.now(timezone.utc) - min_age
    candidates: list[tuple[Version, datetime]] = []
    for ver_str, time_str in releases.items():
        try:
            v = Version(ver_str)
        except InvalidVersion:
            continue
        if v.is_prerelease or v.is_devrelease:
            continue
        upload = parse_iso(time_str)
        if upload <= cutoff:
            candidates.append((v, upload))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])


def main() -> None:
    req_file = sys.argv[1] if len(sys.argv) > 1 else "requirements.txt"

    print(f"[info] reading {req_file}")
    try:
        packages = parse_requirements(req_file)
    except FileNotFoundError:
        print(f"[error] {req_file} not found", file=sys.stderr)
        sys.exit(1)

    if not packages:
        print("[warn] no packages found, nothing to do")
        sys.exit(0)

    print(f"[info] checking {len(packages)} packages")
    print(f"[info] ignoring releases younger than {MIN_AGE_DAYS} days\n")

    print(f"{'Package':<25} {'Current':<15} {'Latest':<15} {'Released':<12} {'Age'}")
    print("-" * 75)

    outdated = 0
    errors = 0
    for name, current in packages:
        releases = fetch_releases(name)
        if releases is None:
            errors += 1
            continue
        latest = find_latest_eligible(releases, timedelta(days=MIN_AGE_DAYS))
        if latest is None:
            continue
        latest_ver, upload = latest
        try:
            cur_ver = Version(current)
        except InvalidVersion:
            cur_ver = None
        if cur_ver is not None and latest_ver <= cur_ver:
            continue

        outdated += 1
        age_days = (datetime.now(timezone.utc) - upload).days
        date_str = upload.strftime("%Y-%m-%d")
        print(f"{name:<25} {current:<15} {str(latest_ver):<15} {date_str:<12} {age_days}d")

    print()
    if outdated:
        print(f"[info] {outdated} package(s) have newer eligible versions")
    else:
        print("[info] all base deps are at the latest eligible version")
    if errors:
        print(f"[warn] {errors} package(s) could not be checked")


if __name__ == "__main__":
    main()
