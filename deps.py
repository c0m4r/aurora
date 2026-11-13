#!/usr/bin/env python3
"""Dependency helper: update checks and security overrides for the uv cooldown.

Aurora never locks anything uploaded to PyPI in the last 7 days
(`[tool.uv] exclude-newer` in pyproject.toml), so a hijacked release has time
to be spotted and yanked before it can reach uv.lock. This script checks for
updates within that window and, when a security fix can't wait, bypasses the
cooldown for a single package via `[tool.uv.exclude-newer-package]`.

Usage:
    ./deps.py [check]                    updates, held-back releases, vulnerabilities
    ./deps.py upgrade [PACKAGE ...]      upgrade all (or some) packages within the cooldown
    ./deps.py security PACKAGE[==VERSION] [-m REASON]
                                         bypass the cooldown for PACKAGE and upgrade it
    ./deps.py prune                      drop overrides the cooldown window has caught up with

`check` exits with 1 when known vulnerabilities are found, so it can gate CI.
Uses only the standard library and the `uv` binary.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("[error] Python 3.11+ required")

import tomllib

ROOT = Path(__file__).resolve().parent
PYPROJECT = ROOT / "pyproject.toml"
LOCKFILE = ROOT / "uv.lock"
OVERRIDES_TABLE = "[tool.uv.exclude-newer-package]"
AUDIT_FLAGS = ("--preview-features", "audit-command,json-output")

# These would silently replace the cooldown configured in pyproject.toml.
IGNORED_ENV = ("UV_EXCLUDE_NEWER", "UV_EXCLUDE_NEWER_PACKAGE")
ENV = {k: v for k, v in os.environ.items() if k not in IGNORED_ENV}


def info(msg: str) -> None:
    print(f"[info] {msg}")


def warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def die(msg: str) -> None:
    print(f"[error] {msg}", file=sys.stderr)
    sys.exit(2)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_date(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def fmt_span(span: timedelta) -> str:
    if span.days and not span.seconds:
        return f"{span.days} day{'s' if span.days != 1 else ''}"
    return str(span)


def normalize(name: str) -> str:
    """PEP 503 name normalization (also a valid bare TOML key)."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --- config -----------------------------------------------------------------

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_FRIENDLY = re.compile(
    r"^\s*(\d+)\s*(s|secs?|seconds?|m|mins?|minutes?|h|hours?|d|days?|w|weeks?)\s*$", re.I
)
_ISO = re.compile(r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", re.I)


def parse_duration(value: str) -> timedelta | None:
    """Parse the subset of uv durations used here ("7 days", "1 week", "P7D")."""
    if m := _FRIENDLY.match(value):
        return timedelta(seconds=int(m[1]) * _UNITS[m[2][0].lower()])
    if (m := _ISO.match(value.strip())) and value.strip().upper() not in ("P", "PT"):
        w, d, h, mi, s = (int(x or 0) for x in m.groups())
        return timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
    return None


def parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


class Config:
    def __init__(self) -> None:
        try:
            data = tomllib.loads(PYPROJECT.read_text())
        except (OSError, tomllib.TOMLDecodeError) as e:
            die(f"cannot read {PYPROJECT.name}: {e}")
        uv = data.get("tool", {}).get("uv", {})
        raw = uv.get("exclude-newer")
        cooldown = parse_duration(raw) if isinstance(raw, str) else None
        if cooldown is None:
            die(f"[tool.uv] exclude-newer must be a relative duration like \"7 days\", got {raw!r}")
        self.cooldown: timedelta = cooldown
        self.required_uv: str | None = uv.get("required-version")
        # Values are timestamps when managed by this script; anything else
        # (false, a duration) is left alone and never pruned.
        self.overrides: dict[str, str | bool] = {
            normalize(k): v for k, v in uv.get("exclude-newer-package", {}).items()
        }
        deps = data.get("project", {}).get("dependencies", [])
        self.direct = {normalize(re.split(r"[\[<>=!~;\s]", d, maxsplit=1)[0]) for d in deps}

    @property
    def window_start(self) -> datetime:
        """Uploads after this moment are inside the cooldown."""
        return now() - self.cooldown

    def override_cutoff(self, name: str) -> datetime | None:
        value = self.overrides.get(name)
        return parse_ts(value) if isinstance(value, str) else None


# --- uv ---------------------------------------------------------------------


def uv(*args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", *args], cwd=ROOT, env=ENV, text=True, capture_output=capture, check=False
    )


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def preflight(cfg: Config) -> None:
    if not shutil.which("uv"):
        die("uv not found, see https://docs.astral.sh/uv/getting-started/installation/")
    if not LOCKFILE.exists():
        die("uv.lock not found; run: uv lock")
    for var in IGNORED_ENV:
        if var in os.environ:
            warn(f"ignoring ${var}, the cooldown comes from pyproject.toml")
    # An uv too old to parse [tool.uv] would warn and resolve with no cooldown.
    have = re.search(r"(\d+(?:\.\d+)+)", uv("--version").stdout)
    need = re.fullmatch(r"\s*>=\s*(\d+(?:\.\d+)*)\s*", cfg.required_uv or "")
    if have and need and version_tuple(have[1]) < version_tuple(need[1]):
        die(f"uv {need[1]}+ required, found {have[1]} (try: uv self update)")


def _comparable(value: object) -> object:
    ts = parse_ts(value) if isinstance(value, str) else None
    return fmt_ts(ts) if ts else value


def verify_lock(cfg: Config) -> None:
    """Refuse to continue unless uv.lock was resolved with our cooldown."""
    options = tomllib.loads(LOCKFILE.read_text()).get("options", {})
    span = parse_duration(options.get("exclude-newer-span", ""))
    if span != cfg.cooldown:
        die(
            f"uv.lock records cooldown {options.get('exclude-newer-span')!r}, expected "
            f"{fmt_span(cfg.cooldown)}; your uv may be ignoring [tool.uv]. "
            "Restore with: git checkout uv.lock"
        )
    locked = {normalize(k): _comparable(v) for k, v in options.get("exclude-newer-package", {}).items()}
    if locked != {k: _comparable(v) for k, v in cfg.overrides.items()}:
        die("uv.lock per-package overrides differ from pyproject.toml; run: uv lock")


def locked_versions() -> dict[str, list[str]]:
    versions: dict[str, list[str]] = {}
    for pkg in tomllib.loads(LOCKFILE.read_text()).get("package", []):
        versions.setdefault(pkg["name"], []).append(pkg["version"])
    return versions


# Dry runs print "Update x v1 -> v2", real runs "Updated x v1 -> v2".
_CHANGE = re.compile(r"^(Update|Add|Remove)(?:d|ed)? (\S+) v(.+?)(?: -> v(.+))?$")


def parse_changes(output: str) -> dict[str, tuple[str, str, str]]:
    """Parse uv's lock change lines into {name: (action, old, new)}."""
    changes: dict[str, tuple[str, str, str]] = {}
    for line in output.splitlines():
        if m := _CHANGE.match(line.strip()):
            action, name, a, b = (g.replace(", v", ", ") if g else g for g in m.groups())
            if action == "Update":
                changes[name] = (action, a, b)
            elif action == "Add":
                changes[name] = (action, "", a)
            else:
                changes[name] = (action, a, "")
    return changes


def lock(*args: str, dry_run: bool = False) -> dict[str, tuple[str, str, str]]:
    proc = uv("lock", *args, *(["--dry-run"] if dry_run else []))
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        die(f"uv lock {' '.join(args)} failed")
    return parse_changes(proc.stderr + proc.stdout)


def sync() -> None:
    info("syncing .venv with uv.lock")
    if uv("sync", "--locked", capture=False).returncode != 0:
        die("uv sync failed")


def audit() -> dict | None:
    proc = uv("audit", "--frozen", *AUDIT_FLAGS, "--output-format", "json")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(proc.stderr)
        warn("uv audit failed, vulnerability status unknown")
        return None


# --- PyPI -------------------------------------------------------------------


def upload_times(name: str, version: str) -> list[datetime]:
    """Upload times of every file of a release (uv's cooldown is per file)."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    req = urllib.request.Request(url, headers={"User-Agent": "aurora-deps"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            files = json.load(resp).get("urls", [])
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        warn(f"could not fetch {name}=={version} from PyPI: {e}")
        return []
    return [t for f in files if (t := parse_ts(f["upload_time_iso_8601"].replace("Z", "+00:00")))]


# --- pyproject.toml editing -------------------------------------------------
# tomllib is read-only, so overrides are edited as text inside their own table
# and the result is validated with tomllib before it is written.

_ENTRY = re.compile(r'^\s*"?([A-Za-z0-9._-]+)"?\s*=')


def _section(lines: list[str]) -> tuple[int, int]:
    """Return [start, end) line indexes of the overrides table body, adding it if missing."""
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == OVERRIDES_TABLE) + 1
    except StopIteration:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["\n", OVERRIDES_TABLE + "\n"]
        start = len(lines)
    end = next((i for i in range(start, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    return start, end


def edit_overrides(set_: dict[str, str] | None = None, remove: set[str] = frozenset()) -> None:
    """Add/replace (`set_` maps name to a full TOML line) and remove override entries."""
    set_ = set_ or {}
    touched = set(remove) | set(set_)
    lines = PYPROJECT.read_text().splitlines(keepends=True)
    start, end = _section(lines)
    body = [
        l for l in lines[start:end]
        if not ((m := _ENTRY.match(l)) and normalize(m[1]) in touched)
    ]
    # Keep new entries above any trailing blank lines of the table.
    insert_at = len(body)
    while insert_at and not body[insert_at - 1].strip():
        insert_at -= 1
    body[insert_at:insert_at] = [line + "\n" for line in set_.values()]
    text = "".join(lines[:start] + body + lines[end:])

    try:
        table = tomllib.loads(text)["tool"]["uv"].get("exclude-newer-package", {})
    except tomllib.TOMLDecodeError as e:
        die(f"refusing to write invalid pyproject.toml: {e}")
    if not set_.keys() <= {normalize(k) for k in table} or remove & {normalize(k) for k in table}:
        die("pyproject.toml edit did not apply cleanly, check [tool.uv.exclude-newer-package]")
    PYPROJECT.write_text(text)


class Rollback:
    """Restore pyproject.toml and uv.lock if anything below fails."""

    def __enter__(self) -> None:
        self.saved = {p: p.read_bytes() for p in (PYPROJECT, LOCKFILE)}

    def __exit__(self, exc_type, *_) -> None:
        if exc_type is not None:
            for path, data in self.saved.items():
                path.write_bytes(data)
            warn("restored pyproject.toml and uv.lock")


# --- commands ---------------------------------------------------------------


def print_changes(changes: dict[str, tuple[str, str, str]], direct: set[str]) -> None:
    for name, (action, old, new) in sorted(changes.items()):
        tag = " (direct)" if name in direct else ""
        if action == "Update":
            print(f"  {name:<24} {old:<16} -> {new}{tag}")
        elif action == "Add":
            print(f"  {name:<24} {'(new dependency)':<16} -> {new}")
        else:
            print(f"  {name:<24} {old:<16} -> (removed, no longer needed)")


def print_vulns(report: dict, only: str | None = None) -> int:
    """Print vulnerabilities (optionally for one package); return how many."""
    by_pkg: dict[tuple[str, str], list[dict]] = {}
    for v in report.get("vulnerabilities", []):
        dep = v.get("dependency", {})
        if only is None or normalize(dep.get("name", "")) == only:
            by_pkg.setdefault((dep.get("name", "?"), dep.get("version", "?")), []).append(v)
    for (name, version), vulns in sorted(by_pkg.items()):
        fixes = sorted({f for v in vulns for f in v.get("fix_versions", [])})
        print(f"  {name} {version}: {len(vulns)} known vulnerabilit{'y' if len(vulns) == 1 else 'ies'}")
        for v in vulns:
            ids = ", ".join([v.get("display_id") or v.get("id", "?"), *v.get("aliases", [])])
            fixed = ", ".join(v.get("fix_versions", [])) or "no fix yet"
            print(f"    - {ids} (fixed in {fixed})")
            print(f"      {v.get('summary', '')}")
        if fixes:
            print(f"    fix: ./deps.py security {name}[==VERSION]  (fixed in {', '.join(fixes)})")
    if only is None:
        for status in report.get("adverse_statuses", []):
            print(f"  adverse status: {json.dumps(status)}")
    return sum(len(v) for v in by_pkg.values())


def cmd_check(cfg: Config, _: argparse.Namespace) -> int:
    if uv("lock", "--check").returncode != 0:
        warn("uv.lock is out of date with pyproject.toml; run: uv lock")
    else:
        verify_lock(cfg)

    info(f"cooldown: {fmt_span(cfg.cooldown)} (uploads after {fmt_ts(cfg.window_start)} are held back)")
    if cfg.overrides:
        print("\nCooldown overrides:")
        for name, value in sorted(cfg.overrides.items()):
            cutoff = cfg.override_cutoff(name)
            if cutoff is None:
                status = "unmanaged, never pruned"
            elif cutoff <= cfg.window_start:
                status = "expired, the cooldown has caught up; run: ./deps.py prune"
            else:
                status = f"active until {fmt_date(cutoff + cfg.cooldown)}"
            print(f"  {name:<24} {value!s:<22} {status}")

    available = lock("--upgrade", dry_run=True)
    newest = lock("--upgrade", "--exclude-newer", "false", dry_run=True)

    print("\nUpdates available outside the cooldown (apply: ./deps.py upgrade [PACKAGE ...]):")
    if available:
        print_changes(available, cfg.direct)
    else:
        print("  none, everything is at the latest eligible version")

    # A package can resolve to several versions (one per Python/platform fork).
    held = {
        name: [v for v in new.split(", ") if v not in available.get(name, ("", "", ""))[2].split(", ")]
        for name, (action, _, new) in newest.items() if action != "Remove"
    }
    if any(held.values()):
        print("\nHeld back by the cooldown (newer than the window):")
        for name, versions in sorted(held.items()):
            for version in versions:
                # uv admits a release once its first file leaves the window.
                times = upload_times(name, version)
                when = f"eligible {fmt_date(min(times) + cfg.cooldown)}" if times else ""
                print(f"  {name:<24} {version:<16} {when}")

    print("\nKnown vulnerabilities (uv audit):")
    if (report := audit()) is None:
        print("  unknown, the audit failed")
        return 2
    found = print_vulns(report)
    if not found:
        print("  none")
    return 1 if found else 0


def cmd_upgrade(cfg: Config, args: argparse.Namespace) -> int:
    verify_lock(cfg)
    flags = [x for p in args.packages for x in ("--upgrade-package", p)] or ["--upgrade"]
    plan = lock(*flags, dry_run=True)
    if not plan:
        info("nothing to upgrade within the cooldown window")
        return 0
    print("Planned changes:")
    print_changes(plan, cfg.direct)
    if not args.yes:
        if not sys.stdin.isatty():
            die("not a terminal; pass --yes to apply")
        if input("\nApply? [y/N] ").strip().lower() not in ("y", "yes"):
            info("aborted, nothing changed")
            return 1
    with Rollback():
        changes = lock(*flags)
        verify_lock(cfg)
    print("\nLocked:")
    print_changes(changes, cfg.direct)
    if not args.no_sync:
        sync()
    info("review and commit pyproject.toml and uv.lock")
    return 0


def cmd_security(cfg: Config, args: argparse.Namespace) -> int:
    verify_lock(cfg)
    m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:==([A-Za-z0-9.+!_-]+))?", args.spec.strip())
    if not m:
        die(f"expected PACKAGE or PACKAGE==VERSION, got {args.spec!r}")
    name, version = normalize(m[1]), m[2]
    if name not in locked_versions():
        die(f"{name} is not a dependency of this project")

    if version:
        # Admit exactly up to this release, not whatever was uploaded after it.
        times = upload_times(name, version)
        if not times:
            die(f"{name}=={version} not found on PyPI")
        cutoff = (max(times) + timedelta(seconds=1)).replace(microsecond=0)
    else:
        cutoff = now()

    reason = " ".join((args.reason or "").split())
    spec = f"{name}=={version}" if version else name
    with Rollback():
        if cutoff <= cfg.window_start:
            info(f"{spec} is already outside the cooldown window, no override needed")
        else:
            previous = cfg.overrides.get(name)
            comment = f"added {fmt_date(now())}" + (f": {reason}" if reason else "")
            edit_overrides(set_={name: f'{name} = "{fmt_ts(cutoff)}"  # {comment}'})
            cfg = Config()
            info(
                f"cooldown override: {name} may use uploads up to {fmt_ts(cutoff)}"
                + (f" (was {previous})" if previous is not None else "")
            )
            info(f"it expires on {fmt_date(cutoff + cfg.cooldown)}; remove it then with ./deps.py prune")
        changes = lock("--upgrade-package", spec)
        verify_lock(cfg)

    if changes:
        print("\nLocked:")
        print_changes(changes, cfg.direct)
    else:
        info(f"{name} unchanged at {', '.join(locked_versions()[name])}")
    if not args.no_sync:
        sync()

    print(f"\nRemaining vulnerabilities in {name}:")
    if (report := audit()) is None:
        print("  unknown, the audit failed")
    elif not print_vulns(report, only=name):
        print("  none known")
    info("review and commit pyproject.toml and uv.lock")
    return 0


def cmd_prune(cfg: Config, _: argparse.Namespace) -> int:
    verify_lock(cfg)
    expired = {
        name for name in cfg.overrides
        if (cutoff := cfg.override_cutoff(name)) and cutoff <= cfg.window_start
    }
    if not expired:
        info("no expired overrides")
        return 0
    before = locked_versions()
    with Rollback():
        edit_overrides(remove=expired)
        cfg = Config()
        lock()
        verify_lock(cfg)
        if locked_versions() != before:
            die("pruning changed locked versions, aborting")
    info(f"removed expired overrides: {', '.join(sorted(expired))}")
    info("review and commit pyproject.toml and uv.lock")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check and update dependencies under the uv supply-chain cooldown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1],
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="show updates, held-back releases and vulnerabilities")
    p = sub.add_parser("upgrade", help="upgrade packages within the cooldown window")
    p.add_argument("packages", nargs="*", metavar="PACKAGE")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.add_argument("--no-sync", action="store_true", help="only update uv.lock, not .venv")
    p = sub.add_parser("security", help="bypass the cooldown for one package and upgrade it")
    p.add_argument("spec", metavar="PACKAGE[==VERSION]")
    p.add_argument("-m", "--reason", help="note stored next to the override, e.g. a CVE id")
    p.add_argument("--no-sync", action="store_true", help="only update uv.lock, not .venv")
    sub.add_parser("prune", help="remove overrides that the cooldown window has caught up with")
    args = parser.parse_args()
    # Keep our lines ordered with uv's (unbuffered) output when piped.
    sys.stdout.reconfigure(line_buffering=True)

    cfg = Config()
    preflight(cfg)
    commands = {"upgrade": cmd_upgrade, "security": cmd_security, "prune": cmd_prune}
    return commands.get(args.command, cmd_check)(cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
