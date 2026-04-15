"""Server probing tool for ping and SSH connectivity checks.

Allows the model to probe servers defined in config to check:
- ICMP ping connectivity
- SSH service availability and authentication
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .base import BaseTool, ToolDefinition

logger = logging.getLogger(__name__)


class ServerProbeTool(BaseTool):
    """Tool for probing server connectivity (ping) and SSH accessibility."""

    def __init__(self, hosts: list[dict], ssh_enabled: bool = False):
        self._hosts: dict[str, dict] = {}
        for h in hosts:
            name = h.get("name") or h.get("host", "unknown")
            self._hosts[name] = h
        self._ssh_enabled = ssh_enabled

    def definition(self) -> ToolDefinition:
        host_names = list(self._hosts.keys())
        capabilities = ["ping"]
        if self._ssh_enabled:
            capabilities.append("ssh")

        return ToolDefinition(
            name="server_probe",
            description=(
                f"Probe servers for connectivity and SSH access, or list configured servers.\n"
                f"Available hosts: {', '.join(host_names) or 'none configured'}.\n"
                f"Supported probes: {', '.join(capabilities)}.\n\n"
                "Use this to:\n"
                "- 'list': List all configured servers with their names, IPs, ports, and users\n"
                "- 'ping': Check if a server is reachable via ICMP ping\n"
                + ("- 'ssh': Check SSH service and authentication\n" if self._ssh_enabled else "")
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": (
                            "Target host to probe. One of: " + (", ".join(host_names)) + 
                            ". Use 'all' with probe_type='list' to show all servers."
                        ),
                        "enum": host_names + ["all"] if host_names else ["(none configured)"],
                    },
                    "probe_type": {
                        "type": "string",
                        "description": (
                            "Type of probe to perform:\n"
                            "- 'list': List all configured servers (use host='all')\n"
                            "- 'ping': Send ICMP echo requests (checks network connectivity)\n"
                            + ("- 'ssh': Check SSH service and authentication\n" if self._ssh_enabled else "")
                        ),
                        "enum": ["list", "ping", "ssh"] if self._ssh_enabled else ["list", "ping"],
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of ping packets to send (for ping probe). Default: 3.",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["host", "probe_type"],
            },
        )

    async def execute(self, host: str, probe_type: str, count: int = 3, **_) -> str:
        """Execute the probe and return results."""
        if probe_type == "list":
            return self._list_servers()

        host_cfg = self._hosts.get(host)
        if not host_cfg:
            return f"Unknown host '{host}'. Configured hosts: {list(self._hosts.keys())}"

        probe_func = self._probe_ssh if probe_type == "ssh" else self._probe_ping
        try:
            result = await probe_func(host, host_cfg, count)
            return result
        except Exception as exc:
            logger.error("Probe failed on %s: %s", host, exc)
            return f"[PROBE ERROR] {exc}"

    def _list_servers(self) -> str:
        """List all configured servers with their details."""
        if not self._hosts:
            return "No servers configured."

        parts = ["=== Configured Servers ===", ""]

        for name, cfg in self._hosts.items():
            host_addr = cfg.get("host", "unknown")
            port = cfg.get("port", 22)
            user = cfg.get("user", "root")
            key_file = cfg.get("key_file", "(not configured)")

            parts.append(f"Name:     {name}")
            parts.append(f"Address:  {host_addr}")
            parts.append(f"Port:     {port}")
            parts.append(f"User:     {user}")
            parts.append(f"Key File: {key_file}")
            parts.append("")

        parts.append(f"Total: {len(self._hosts)} server(s) configured")
        return "\n".join(parts)

    async def _probe_ping(self, host_name: str, host_cfg: dict, count: int) -> str:
        """Perform ICMP ping probe."""
        target = host_cfg.get("host", host_name)
        count = min(max(int(count), 1), 10)  # Clamp between 1 and 10

        parts = [f"=== Ping Probe: {host_name} ({target}) ==="]

        # Determine ping command based on OS
        import platform
        if platform.system().lower() == "windows":
            cmd = ["ping", "-n", str(count), target]
        else:
            # Linux/macOS: use -c for count, -W for timeout (2 seconds)
            cmd = ["ping", "-c", str(count), "-W", "2", target]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=count * 3 + 10)

            if proc.returncode == 0:
                output = stdout.decode().strip()
                parts.append(output)
                parts.append(f"\n[SUCCESS] Host {host_name} is reachable via ping")
            else:
                error_output = stderr.decode().strip() if stderr else ""
                parts.append(f"[FAILURE] Host {host_name} is NOT reachable via ping")
                if error_output:
                    parts.append(f"Error: {error_output[:500]}")
        except asyncio.TimeoutError:
            parts.append(f"[TIMEOUT] Ping probe exceeded timeout for {host_name}")
        except FileNotFoundError:
            parts.append("[ERROR] ping command not found. Ensure iputils-ping is installed.")
        except Exception as exc:
            parts.append(f"[ERROR] Ping probe failed: {exc}")

        return "\n".join(parts)

    async def _probe_ssh(self, host_name: str, host_cfg: dict, _count: int) -> str:
        """Perform SSH connectivity and authentication probe."""
        target = host_cfg.get("host", host_name)
        port = int(host_cfg.get("port", 22))
        username = host_cfg.get("user", "root")

        parts = [f"=== SSH Probe: {host_name} ({target}:{port}) ==="]
        parts.append(f"User: {username}")

        if not self._ssh_enabled:
            parts.append("[DISABLED] SSH probing is not enabled in config")
            return "\n".join(parts)

        try:
            import asyncssh
        except ImportError:
            parts.append("[ERROR] asyncssh is not installed. Run: pip install asyncssh")
            return "\n".join(parts)

        # Build connection parameters
        connect_kw: dict[str, Any] = {
            "host": target,
            "port": port,
            "username": username,
            "known_hosts": None,  # Accept any host key (same as SSHTool)
        }

        key_file = host_cfg.get("key_file")
        if key_file:
            from pathlib import Path
            connect_kw["client_keys"] = [str(Path(key_file).expanduser())]
        if host_cfg.get("password"):
            connect_kw["password"] = host_cfg["password"]

        start_time = time.time()
        try:
            # Attempt SSH connection with 10s timeout
            async with asyncio.timeout(10):
                async with asyncssh.connect(**connect_kw) as conn:
                    elapsed = time.time() - start_time
                    parts.append(f"\n[SUCCESS] SSH connection established in {elapsed:.3f}s")

                    # Get server version info if available
                    server_version = conn.get_extra_info('server_version', None)
                    if server_version:
                        parts.append(f"Server version: {server_version}")

                    # Try to run a simple command to verify shell access
                    result = await asyncio.wait_for(
                        conn.run("echo 'SSH_PROBE_OK'", check=False),
                        timeout=5.0,
                    )

                    if result.stdout.strip() == "SSH_PROBE_OK":
                        parts.append("[SUCCESS] Shell access verified - can execute commands")
                    else:
                        parts.append("[WARNING] Shell access test returned unexpected output")

                    parts.append(f"Exit status: {result.exit_status}")

        except asyncssh.PermissionDenied as exc:
            elapsed = time.time() - start_time
            parts.append(f"\n[FAILURE] SSH authentication failed after {elapsed:.3f}s")
            parts.append(f"Error: Permission denied - {exc}")
            parts.append("Check username, key file, or password configuration")

        except (OSError, ConnectionError) as exc:
            elapsed = time.time() - start_time
            parts.append(f"\n[FAILURE] SSH connection refused/failed after {elapsed:.3f}s")
            parts.append(f"Error: {exc}")
            parts.append("Verify SSH service is running on the target host")

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            parts.append(f"\n[TIMEOUT] SSH probe exceeded timeout ({elapsed:.3f}s elapsed)")

        except Exception as exc:
            elapsed = time.time() - start_time
            parts.append(f"\n[ERROR] SSH probe failed after {elapsed:.3f}s")
            parts.append(f"Error: {exc}")

        return "\n".join(parts)
