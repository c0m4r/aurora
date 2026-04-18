"""SCP upload tool — upload files from ./files/ sandbox to remote servers via SCP."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .base import BaseTool, ToolDefinition
from .sandbox import resolve as _resolve
from ._ssh_common import build_connect_kwargs, host_key_error_hint

logger = logging.getLogger(__name__)


class SCPUploadTool(BaseTool):
    def __init__(self, hosts: list[dict]):
        self._hosts: dict[str, dict] = {}
        for h in hosts:
            name = h.get("name") or h.get("host", "unknown")
            self._hosts[name] = h

    def definition(self) -> ToolDefinition:
        host_names = list(self._hosts.keys())
        return ToolDefinition(
            name="scp_upload",
            description=(
                "Upload a file from the local ./files/ sandbox to a remote server via SCP. "
                f"Available hosts: {', '.join(host_names) or 'none configured'}.\n\n"
                "Only files that exist inside ./files/ can be uploaded — you cannot upload "
                "arbitrary system files. Use this to share files you've created or saved "
                "in the sandbox with remote servers.\n\n"
                "The destination path is relative to the user's home directory on the remote "
                "server. Use absolute paths (starting with /) if you need a specific location."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": f"Target host. One of: {', '.join(host_names)}",
                        "enum": host_names if host_names else ["(none configured)"],
                    },
                    "source": {
                        "type": "string",
                        "description": (
                            "Relative path inside ./files/ of the file to upload. "
                            "Examples: 'report.md', 'output/logo.svg', 'data/results.json'."
                        ),
                    },
                    "destination": {
                        "type": "string",
                        "description": (
                            "Destination path on the remote server. "
                            "Relative paths go to the user's home directory. "
                            "Absolute paths (starting with /) go to the specified location. "
                            "Examples: '~/uploads/report.md', '/var/www/html/index.html'."
                        ),
                    },
                },
                "required": ["host", "source", "destination"],
            },
        )

    async def execute(
        self, host: str, source: str, destination: str, **_
    ) -> str:
        host_cfg = self._hosts.get(host)
        if not host_cfg:
            return f"Unknown host '{host}'. Configured hosts: {list(self._hosts.keys())}"

        if not source or not source.strip():
            return "Error: source path must not be empty."

        if not destination or not destination.strip():
            return "Error: destination path must not be empty."

        # Resolve source inside sandbox
        src_path = _resolve(source)
        if src_path is None:
            return "[BLOCKED] Source path traversal outside ./files/ is not allowed."

        if not src_path.exists():
            return f"Not found: files/{source}"

        if not src_path.is_file():
            return f"Not a file: files/{source} (it's a directory)."

        try:
            import asyncssh
        except ImportError:
            return "asyncssh is not installed. Run: pip install asyncssh"

        connect_kw = build_connect_kwargs(host_cfg, host)

        try:
            async with asyncssh.connect(**connect_kw) as conn:
                sftp = await conn.start_sftp_client()

                # Ensure remote parent directory exists
                remote_parent = str(Path(destination).parent)
                if remote_parent and remote_parent != ".":
                    try:
                        await sftp.mkdir(remote_parent)
                    except asyncssh.SFTPError:
                        pass  # directory likely already exists — ignore

                await sftp.put(str(src_path), destination)

                file_size = src_path.stat().st_size
                return (
                    f"Uploaded files/{source} ({_human_size(file_size)}) "
                    f"to {host}:{destination}"
                )
        except asyncio.TimeoutError:
            return f"[TIMEOUT] Upload to {host} exceeded the timeout."
        except Exception as exc:
            if isinstance(exc, asyncssh.HostKeyNotVerifiable):
                return "[BLOCKED — " + host_key_error_hint(host, exc) + "]"
            logger.warning("SCP upload failed to %s: %s", host, exc)
            return f"SCP error on {host}: {exc}"


def _human_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"
