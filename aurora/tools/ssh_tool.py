"""General-purpose SSH tool.

Read-only by default — the model gathers information without touching state.
Write mode is enabled per-host in config (allow_writes: true) and the model
must only activate write commands when the user has explicitly requested a change.

Catastrophic operations (rm -rf /, mkfs, dd to device nodes, fork bombs, etc.)
are unconditionally blocked regardless of mode.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from .base import BaseTool, ToolDefinition

logger = logging.getLogger(__name__)

# ─── Safety patterns ──────────────────────────────────────────────────────────

# Shell evasion patterns — attempts to obfuscate commands
_EVASION_PATTERNS = re.compile(
    r"""
    \$'\\.+                        # $'\x72\x6d' ANSI-C quoting for hex/octal
    | \$\(.*\b(?:base64|xxd)\b     # $(echo cm0= | base64 -d) decode tricks
    | \bbase64\s+-d\b              # base64 decode piped to shell
    | \bxxd\s+-r\b                 # xxd reverse (hex to binary)
    | \beval\b                     # eval arbitrary string as command
    | \bexec\b\s+\d*[<>]          # exec with redirections (fd manipulation)
    | \bsource\b                   # source a script
    | \bpython[23]?\s+-c\b        # python -c 'import os; os.system(...)'
    | \bperl\s+-e\b               # perl -e 'system(...)'
    | \bruby\s+-e\b               # ruby -e '`rm ...`'
    | \blua\s+-e\b                # lua -e 'os.execute(...)'
    | \bphp\s+-r\b                # php -r 'system(...)'
    | \bnohup\b                   # nohup — survives session close
    | \bdisown\b                  # disown — detach from shell
    | \bsetsid\b                  # setsid — new session leader
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Blocked in ALL modes — irreversible / catastrophic
_ALWAYS_BLOCKED = re.compile(
    r"""
    \brm\s+(?:-[a-zA-Z]*\s+)*(?:--\s+)?/(?:\s|$)  # rm -rf / or rm /
    | \bmkfs\b
    | \bwipefs\b
    | \bshred\b.*\s/dev/
    | \bdd\b.*\bof=/dev/(?!null|zero|random|urandom)  # dd to real block dev
    | :\s*\(\s*\)\s*\{.*:\|:.*\}                   # fork bomb
    | \bchroot\s+/\s                                # chroot to root
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Blocked in READ-ONLY mode — commands that modify system state
_WRITE_COMMANDS = re.compile(
    r"""
    # Redirects that overwrite / append files
    (?<![<2])\s?>(?!=)          # > but not => or 2>
    | >>                        # append redirect
    | \|\s*tee\b                # pipe to tee (writes file)

    # File modification
    | \brm\s+-[a-zA-Z]*[rf]    # rm -r, rm -f, rm -rf
    | \brm\b.*\s(?!.*\becho\b) # plain rm
    | \bmv\b
    | \bchmod\b | \bchown\b | \bchgrp\b
    | \btouch\b | \btruncate\b
    | \bln\s+-[sf]              # symlinks
    | \bmkdir\b | \brmdir\b

    # Package managers (install/remove/upgrade)
    | \bapt(?:-get)?\s+(?:install|remove|purge|upgrade|dist-upgrade|autoremove)\b
    | \byum\s+(?:install|remove|erase|update|upgrade)\b
    | \bdnf\s+(?:install|remove|erase|update|upgrade)\b
    | \bpacman\s+--?[SRU]\b
    | \bzypper\s+(?:install|remove|update|upgrade)\b
    | \bpip3?\s+install\b
    | \bnpm\s+(?:install|i|ci|uninstall)\b
    | \bcargo\s+install\b

    # Service control (state-changing only)
    | \bsystemctl\s+(?:start|stop|restart|reload|enable|disable|mask|unmask|daemon-reload)\b
    | \bservice\s+\S+\s+(?:start|stop|restart|reload)\b

    # User and group management
    | \buseradd\b | \buserdel\b | \busermod\b
    | \bgroupadd\b | \bgroupdel\b | \bgroupmod\b
    | (?:^|[;&|]\s*)\bpasswd\b(?!\s*--status)  # passwd command (not /etc/passwd path)
    | \bchpasswd\b | \bchage\b

    # Firewall
    | \biptables\s+-[AIDFPNXZ]\b | \bip6tables\s+-[AIDFPNXZ]\b
    | \bnftables?\b.*\badd\b
    | \bufw\s+(?:allow|deny|delete|enable|disable|reset)\b
    | \bfirewall-cmd\b.*--(?:add|remove|set|change)

    # Network configuration
    | \bip\s+(?:link|addr|route|rule)\s+(?:add|del|change|set|flush)\b
    | \bifconfig\b.*(?:up|down|netmask|broadcast)
    | \bnmcli\b.*(?:add|modify|delete|up|down)\b

    # Mounts
    | \bmount\b(?!\s+--show|\s+-l|\s+--help)
    | \bumount\b

    # Cron / scheduled tasks
    | \bcrontab\s+-[re]\b

    # Kernel / boot
    | \bsysctl\s+-w\b
    | \bmodprobe\b(?!\s+-l|\s+-n|\s+--show)
    | \brmmod\b | \binsmod\b

    # Init / power
    | \breboot\b | \bshutdown\b | \bhalt\b | \bpoweroff\b
    | \binit\s+[0-6]\b | \btelinit\s+[0-6]\b

    # Process management (killing)
    | \bkill\b | \bkillall\b | \bpkill\b
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _is_safe_readonly(command: str) -> tuple[bool, str]:
    """Return (is_allowed, reason). Blocks write operations in read-only mode."""
    if _EVASION_PATTERNS.search(command):
        return False, (
            "command uses shell evasion patterns (encoding, eval, scripting interpreters). "
            "Use plain, readable commands instead."
        )
    if _ALWAYS_BLOCKED.search(command):
        return False, "catastrophic operation unconditionally blocked"
    if _WRITE_COMMANDS.search(command):
        return False, (
            "command contains a write/state-changing operation. "
            "The user must explicitly ask for system changes before you use write mode."
        )
    return True, ""


def _is_safe_write(command: str) -> tuple[bool, str]:
    """Return (is_allowed, reason). Only blocks catastrophic commands."""
    if _EVASION_PATTERNS.search(command):
        return False, (
            "command uses shell evasion patterns (encoding, eval, scripting interpreters). "
            "Use plain, readable commands instead."
        )
    if _ALWAYS_BLOCKED.search(command):
        return False, "catastrophic/irreversible operation is always blocked for safety"
    return True, ""


class SSHTool(BaseTool):
    def __init__(self, hosts: list[dict], allow_writes: bool = False):
        self._hosts: dict[str, dict] = {}
        for h in hosts:
            name = h.get("name") or h.get("host", "unknown")
            self._hosts[name] = h
        self._allow_writes = allow_writes

    def definition(self) -> ToolDefinition:
        host_names = list(self._hosts.keys())
        mode_note = (
            "**Write mode is enabled** — you may run state-changing commands, "
            "but only when the user has explicitly asked for a system change. "
            "Always state what the command will do before running it."
            if self._allow_writes
            else "**Read-only mode** — information gathering only. "
            "Write/modification commands are blocked. "
            "If the user asks you to make a change, inform them that write mode "
            "must be enabled in config (allow_writes: true) and ask them to confirm."
        )
        return ToolDefinition(
            name="ssh",
            description=(
                f"Run shell commands on remote servers via SSH.\n"
                f"Available hosts: {', '.join(host_names) or 'none configured'}.\n"
                f"{mode_note}\n\n"
                "Use this for: system info (uname, uptime, free, df), process inspection "
                "(ps, top, pgrep), log reading (journalctl, tail, grep), network state "
                "(ss, ip addr, ping), service status (systemctl status), file browsing, "
                "docker/kubernetes inspection, and other tasks. "
                "When using SSH to run commands on remote servers, you MUST always use **non-interactive** methods. "
                "Never run commands that require user input or that will hang indefinitely. "
                "Examples: "
                "- Use `apt-get -y` instead of `apt` for unattended operations (e.g., `apt-get -y full-upgrade`) "
                "- Use `-y` flag to auto-confirm prompts for apt, yum, dnf, etc. or --noconfirm for pacman "
                "- For dpkg, use `DEBIAN_FRONTEND=noninteractive` when needed "
                "- Example: `DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade` "
                "- Never run interactive tools like `top`, `htop`, `less`, `vim` directly, "
                "use `timeout` to prevent hanging: `timeout 2 top -d1` or `top -d1 -n1` (exit after 1 iteration)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": f"Target host. One of: {', '.join(host_names)}",
                        "enum": host_names if host_names else ["(none configured)"],
                    },
                    "command": {
                        "type": "string",
                        "description": (
                            "Shell command to execute. "
                            "Pipe, subshells, and multi-statement commands (&&, ;) are fine. "
                            "Examples: "
                            "'journalctl -u nginx --since \"10 min ago\" --no-pager | tail -50', "
                            "'ps aux --sort=-%cpu | head -20', "
                            "'df -hT && free -h', "
                            "'find /var/log -name \"*.log\" -newer /tmp -ls'"
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Command timeout in seconds (default: 60, max: 300).",
                        "default": 60,
                    },
                },
                "required": ["host", "command"],
            },
        )

    async def execute(self, host: str, command: str, timeout: int = 60, **_) -> str:
        host_cfg = self._hosts.get(host)
        if not host_cfg:
            return f"Unknown host '{host}'. Configured hosts: {list(self._hosts.keys())}"

        # Per-host write override
        host_writes = host_cfg.get("allow_writes", self._allow_writes)

        # Safety check
        if host_writes:
            ok, reason = _is_safe_write(command)
        else:
            ok, reason = _is_safe_readonly(command)

        if not ok:
            return f"[BLOCKED — {reason}]\nCommand: {command}"

        try:
            import asyncssh
        except ImportError:
            return "asyncssh is not installed. Run: pip install asyncssh"

        timeout = min(int(timeout or 60), 300)

        connect_kw: dict[str, Any] = {
            "host":      host_cfg.get("host", host),
            "port":      int(host_cfg.get("port", 22)),
            "username":  host_cfg.get("user", "root"),
            "known_hosts": None,  # accept any; tighten with known_hosts_file in production
        }
        key_file = host_cfg.get("key_file")
        if key_file:
            connect_kw["client_keys"] = [str(Path(key_file).expanduser())]
        if host_cfg.get("password"):
            connect_kw["password"] = host_cfg["password"]

        try:
            async with asyncssh.connect(**connect_kw) as conn:
                result = await asyncio.wait_for(
                    conn.run(command, check=False, term_type="dumb"),
                    timeout=float(timeout),
                )
        except asyncio.TimeoutError:
            return f"[TIMEOUT] Command exceeded {timeout}s on {host}"
        except Exception as exc:
            logger.warning("SSH connect failed on %s: %s", host, exc)
            return f"SSH error on {host}: {exc}"

        parts: list[str] = []
        if result.stdout:
            lines = result.stdout.splitlines()
            if len(lines) > 600:
                parts.append("\n".join(lines[:600]))
                parts.append(f"\n[… {len(lines) - 600} more lines truncated]")
            else:
                parts.append(result.stdout.rstrip())
        if result.stderr:
            parts.append(f"\n[stderr]\n{result.stderr[:2000].rstrip()}")
        if result.exit_status not in (0, None):
            parts.append(f"\n[exit status: {result.exit_status}]")

        return "\n".join(parts) if parts else "(no output)"
