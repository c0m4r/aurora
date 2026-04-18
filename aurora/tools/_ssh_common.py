"""Shared SSH connection helpers — host-key verification, key/password wiring."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_KNOWN_HOSTS = "~/.ssh/known_hosts"


def build_connect_kwargs(host_cfg: dict, host_name: str) -> dict[str, Any]:
    """Return asyncssh.connect() kwargs with safe host-key defaults.

    Host-key verification rules (most specific wins):
      1. host_cfg['host_key_fingerprint'] → pin a single fingerprint (any format
         asyncssh accepts for known_hosts lines, e.g. 'ssh-ed25519 AAAA...')
      2. host_cfg['known_hosts_file'] → use that file
      3. host_cfg['insecure_accept_any_host_key'] == True → known_hosts=None
         (dangerous — MitM possible; only for isolated lab environments)
      4. Default → ~/.ssh/known_hosts (asyncssh's strict verification)
    """
    import asyncssh  # late import so this module can load without asyncssh

    connect_kw: dict[str, Any] = {
        "host":     host_cfg.get("host", host_name),
        "port":     int(host_cfg.get("port", 22)),
        "username": host_cfg.get("user", "root"),
    }

    # Host-key verification
    fingerprint = host_cfg.get("host_key_fingerprint")
    known_hosts_file = host_cfg.get("known_hosts_file")
    insecure = bool(host_cfg.get("insecure_accept_any_host_key", False))

    if fingerprint:
        # asyncssh accepts a single known_hosts line as a tuple:
        # (trusted_host_keys, trusted_ca_keys, revoked_keys). A plain
        # authorized_keys-style line is the simplest path.
        connect_kw["known_hosts"] = (
            [asyncssh.import_public_key(fingerprint)],
            [],
            [],
        )
    elif known_hosts_file:
        connect_kw["known_hosts"] = str(Path(known_hosts_file).expanduser())
    elif insecure:
        logger.warning(
            "SSH host %r: insecure_accept_any_host_key=true — host key is NOT verified. "
            "Susceptible to man-in-the-middle attacks.",
            host_name,
        )
        connect_kw["known_hosts"] = None
    else:
        # Default: strict verification against ~/.ssh/known_hosts.
        # asyncssh will raise if the host key is missing/unknown — the operator
        # must TOFU-add it (ssh-keyscan -H host >> ~/.ssh/known_hosts) or pin.
        connect_kw["known_hosts"] = str(Path(_DEFAULT_KNOWN_HOSTS).expanduser())

    key_file = host_cfg.get("key_file")
    if key_file:
        connect_kw["client_keys"] = [str(Path(key_file).expanduser())]
    if host_cfg.get("password"):
        connect_kw["password"] = host_cfg["password"]

    return connect_kw


def host_key_error_hint(host_name: str, exc: BaseException) -> str:
    """Produce a helpful message when host-key verification fails."""
    return (
        f"SSH host-key verification failed for {host_name!r}: {exc}\n"
        "Fixes:\n"
        "  - If you trust this host: add its key to ~/.ssh/known_hosts "
        f"(e.g. `ssh-keyscan -H <hostname> >> ~/.ssh/known_hosts`).\n"
        "  - Or set host_key_fingerprint / known_hosts_file per host in config.yaml.\n"
        "  - For a lab only: set insecure_accept_any_host_key: true on the host (NOT recommended)."
    )
