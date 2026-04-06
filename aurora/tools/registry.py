"""Build the tool registry from config."""
from __future__ import annotations

from typing import Any

from .base import BaseTool


class ToolRegistry:
    def __init__(self, tools: list[BaseTool] | None = None):
        self._tools: dict[str, BaseTool] = {}
        for t in (tools or []):
            self.register(t)

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.definition().name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def all(self) -> list[BaseTool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        return [t.to_dict() for t in self._tools.values()]

    async def execute(self, name: str, **kwargs: Any) -> str:
        tool = self._tools.get(name)
        if not tool:
            return f"Unknown tool: '{name}'. Available: {list(self._tools.keys())}"
        return await tool.execute(**kwargs)


def build_registry(cfg: Any) -> ToolRegistry:
    """Instantiate tools from config and return a ToolRegistry."""
    from .datetime_tool import DateTimeTool
    from .websearch_tool import WebSearchTool
    from .ssh_tool import SSHTool
    from .file_tool import FileReadTool, FileWriteTool
    from .rss_tool import RSSFeedTool
    from .server_probe import ServerProbeTool
    from .scp_upload_tool import SCPUploadTool

    tools: list[BaseTool] = [
        DateTimeTool(),
        FileReadTool(),
        FileWriteTool(),
    ]

    tcfg = getattr(cfg, "tools", None)

    # SSH
    ssh = getattr(tcfg, "ssh", None) if tcfg else None
    if ssh and getattr(ssh, "enabled", False):
        raw_hosts = getattr(ssh, "hosts", []) or []
        host_dicts: list[dict] = []
        for h in raw_hosts:
            if hasattr(h, "__dict__"):
                host_dicts.append({k: v for k, v in h.__dict__.items() if not k.startswith("_")})
            elif isinstance(h, dict):
                host_dicts.append(h)
        if host_dicts:
            allow_writes = bool(getattr(ssh, "allow_writes", False))
            tools.append(SSHTool(host_dicts, allow_writes=allow_writes))

    # SCP Upload — reuses SSH hosts
    scp_cfg = getattr(tcfg, "scp_upload", None) if tcfg else None
    if scp_cfg and getattr(scp_cfg, "enabled", False):
        ssh_hosts_raw = getattr(ssh, "hosts", []) or [] if ssh else []
        ssh_host_dicts: list[dict] = []
        for h in ssh_hosts_raw:
            if hasattr(h, "__dict__"):
                ssh_host_dicts.append({k: v for k, v in h.__dict__.items() if not k.startswith("_")})
            elif isinstance(h, dict):
                ssh_host_dicts.append(h)
        if ssh_host_dicts:
            tools.append(SCPUploadTool(ssh_host_dicts))

    # Server Probe
    probe_cfg = getattr(tcfg, "server_probe", None) if tcfg else None
    if probe_cfg and getattr(probe_cfg, "enabled", False):
        # Reuse SSH hosts for probing
        ssh_hosts_raw = getattr(ssh, "hosts", []) or [] if ssh else []
        ssh_host_dicts: list[dict] = []
        for h in ssh_hosts_raw:
            if hasattr(h, "__dict__"):
                ssh_host_dicts.append({k: v for k, v in h.__dict__.items() if not k.startswith("_")})
            elif isinstance(h, dict):
                ssh_host_dicts.append(h)

        if ssh_host_dicts:
            ssh_probe_enabled = bool(getattr(probe_cfg, "enable_ssh_probe", False))
            tools.append(ServerProbeTool(ssh_host_dicts, ssh_enabled=ssh_probe_enabled))

    # Web search / fetch
    ws = getattr(tcfg, "websearch", None) if tcfg else None
    ws_enabled = getattr(ws, "enabled", True) if ws else True
    if ws_enabled:
        whitelist_raw = getattr(ws, "whitelist", None) if ws else None
        whitelist = list(whitelist_raw) if whitelist_raw else None
        tools.append(WebSearchTool(
            max_results=int(getattr(ws, "max_results", 5)) if ws else 5,
            fetch_content=bool(getattr(ws, "fetch_content", True)) if ws else True,
            max_content_length=int(getattr(ws, "max_content_length", 4000)) if ws else 4000,
            whitelist=whitelist,  # None = use built-in defaults
        ))

    # RSS feed reader
    rss_cfg = getattr(tcfg, "rss", None) if tcfg else None
    rss_enabled = getattr(rss_cfg, "enabled", True) if rss_cfg else True
    if rss_enabled:
        max_items = int(getattr(rss_cfg, "max_items", 10)) if rss_cfg else 10
        extra_raw = getattr(rss_cfg, "extra_feeds", None) if rss_cfg else None
        extra_feeds: dict[str, str] | None = None
        if extra_raw and hasattr(extra_raw, "__dict__"):
            extra_feeds = {k: v for k, v in extra_raw.__dict__.items() if not k.startswith("_")}
        elif isinstance(extra_raw, dict):
            extra_feeds = extra_raw
        tools.append(RSSFeedTool(max_items=max_items, extra_feeds=extra_feeds))

    return ToolRegistry(tools)
