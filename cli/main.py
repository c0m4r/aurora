#!/usr/bin/env python3
"""
Aurora CLI — connects to a remote Aurora server.

Usage:
  aurora                        # interactive REPL
  aurora -m "show disk usage"   # single shot
  aurora --server http://host:8000
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

import httpx
import typer

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.text import Text
    from rich.live import Live
    from rich.columns import Columns
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    print("Install 'rich' for better output: pip install rich", file=sys.stderr)

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.formatted_text import HTML
    HAS_PROMPT = True
except ImportError:
    HAS_PROMPT = False

app = typer.Typer(name="aurora", add_completion=True, help="Aurora CLI client")
console = Console() if HAS_RICH else None


def cprint(*args, **kwargs):
    if console:
        console.print(*args, **kwargs)
    else:
        print(*args)


class Client:
    def __init__(self, server: str, api_key: str = "", model: str = ""):
        self.server = server.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.conv_id: Optional[str] = None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    async def chat(self, message: str, verbose: bool = True) -> str:
        """Send a message, stream and display the response, return full text."""
        payload = {
            "message": message,
            "conversation_id": self.conv_id,
        }
        if self.model:
            payload["model"] = self.model

        full_text = []
        thinking_shown = False

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    f"{self.server}/api/chat/stream",
                    json=payload,
                    headers=self._headers(),
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        cprint(f"[red]Server error {resp.status_code}: {body.decode()[:300]}[/red]")
                        return ""

                    buf = ""
                    async for raw_bytes in resp.aiter_bytes():
                        buf += raw_bytes.decode("utf-8", errors="replace")
                        while "\n\n" in buf:
                            chunk, buf = buf.split("\n\n", 1)
                            chunk = chunk.strip()
                            if not chunk.startswith("data: "):
                                continue
                            data_str = chunk[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                event = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            etype = event.get("type")

                            if etype == "conv_id":
                                self.conv_id = event.get("conversation_id")

                            elif etype == "thinking" and verbose:
                                if not thinking_shown:
                                    thinking_shown = True
                                    cprint("\n[dim italic]💭 Thinking…[/dim italic]")

                            elif etype == "text":
                                full_text.append(event.get("content", ""))
                                print(event.get("content", ""), end="", flush=True)

                            elif etype == "tool_call" and verbose:
                                name = event.get("name", "")
                                inp = event.get("input", {})
                                print()  # newline
                                cprint(f"[cyan]⚙  {name}[/cyan]")
                                for k, v in (inp or {}).items():
                                    vs = str(v)
                                    if len(vs) > 120:
                                        vs = vs[:120] + "…"
                                    cprint(f"   [dim]{k}:[/dim] {vs}")

                            elif etype == "tool_result" and verbose:
                                is_err = event.get("error", False)
                                out = event.get("output", "")
                                name = event.get("name", "")
                                icon = "✗" if is_err else "✓"
                                color = "red" if is_err else "green"
                                preview = "\n".join(out.splitlines()[:4])
                                if len(out.splitlines()) > 4:
                                    preview += f"\n   … ({len(out.splitlines()) - 4} more lines)"
                                cprint(f"[{color}]{icon} {name}:[/{color}] {preview[:400]}")

                            elif etype == "usage" and verbose:
                                itok = event.get("input_tokens", 0)
                                otok = event.get("output_tokens", 0)
                                cprint(f"\n[dim]↑{itok} ↓{otok} tokens[/dim]")

                            elif etype == "error":
                                cprint(f"\n[red]Error: {event.get('content', '')}[/red]")

                            elif etype == "done":
                                break

        except httpx.ConnectError:
            cprint(f"[red]Cannot connect to {self.server}[/red]")
        except Exception as exc:
            cprint(f"[red]Error: {exc}[/red]")

        print()  # final newline
        return "".join(full_text)

    async def list_models(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self.server}/api/models", headers=self._headers())
            resp.raise_for_status()
        return resp.json().get("models", [])

    async def list_conversations(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self.server}/api/conversations", headers=self._headers())
            resp.raise_for_status()
        return resp.json()

    async def get_conversation(self, cid: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self.server}/api/conversations/{cid}", headers=self._headers())
            resp.raise_for_status()
        return resp.json()

    async def interactive(self, verbose: bool = True):
        """REPL loop."""
        hist_path = Path("~/.local/share/aurora/cli_history").expanduser()
        hist_path.parent.mkdir(parents=True, exist_ok=True)

        if HAS_PROMPT:
            session: PromptSession = PromptSession(
                history=FileHistory(str(hist_path)),
                auto_suggest=AutoSuggestFromHistory(),
            )

        banner = (
            "[bold cyan]Aurora[/bold cyan] — general purpose AI assistant\n"
            f"[dim]Server: {self.server}[/dim]\n"
            "[dim]Commands: /models  /use <model>  /history  /load <id>  /new  /quit[/dim]\n"
            "[dim]Tip: Right-click the logo in the web UI to configure settings.[/dim]"
        )
        if console:
            console.print(Panel(banner, border_style="cyan", expand=False))
        else:
            print("Aurora CLI\nCommands: /models /use <model> /history /load <id> /new /quit")

        while True:
            try:
                if HAS_PROMPT:
                    user_input: str = await session.prompt_async(
                        HTML("<ansigreen>You</ansigreen> <ansigray>▶</ansigray> ")
                    )
                else:
                    user_input = input("You > ")
            except (EOFError, KeyboardInterrupt):
                cprint("\n[dim]Goodbye![/dim]")
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input.startswith("/"):
                await self._handle_command(user_input, verbose)
            else:
                cprint("\n[bold]Aurora:[/bold]")
                await self.chat(user_input, verbose=verbose)
                cprint()

    async def _handle_command(self, cmd: str, verbose: bool):
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if command in ("/quit", "/exit", "/q"):
            raise KeyboardInterrupt

        elif command == "/models":
            try:
                models = await self.list_models()
                cprint("\n[bold]Available models:[/bold]")
                for m in models:
                    thinking = " [cyan](thinking)[/cyan]" if m.get("supports_thinking") else ""
                    active = " ◀" if m["id"] == self.model else ""
                    cprint(f"  {m['id']}{thinking}[dim]{active}[/dim]")
            except Exception as exc:
                cprint(f"[red]{exc}[/red]")

        elif command == "/use" and arg:
            self.model = arg
            cprint(f"[green]Switched to model: {arg}[/green]")

        elif command == "/new":
            self.conv_id = None
            cprint("[green]Started new conversation[/green]")

        elif command == "/history":
            try:
                convs = await self.list_conversations()
                cprint("\n[bold]Recent conversations:[/bold]")
                for c in convs[:20]:
                    cprint(f"  [dim]{c['id'][:8]}[/dim]  {c['title'][:60]}  [dim]{c['updated_at'][:10]}[/dim]")
            except Exception as exc:
                cprint(f"[red]{exc}[/red]")

        elif command == "/load" and arg:
            self.conv_id = arg if len(arg) > 8 else None
            # Try to find by prefix
            if len(arg) < 36:
                try:
                    convs = await self.list_conversations()
                    matches = [c for c in convs if c["id"].startswith(arg)]
                    if matches:
                        self.conv_id = matches[0]["id"]
                        cprint(f"[green]Loaded: {matches[0]['title']}[/green]")
                    else:
                        cprint("[yellow]No matching conversation[/yellow]")
                except Exception as exc:
                    cprint(f"[red]{exc}[/red]")

        else:
            cprint("[yellow]Unknown command. Available: /models /use <model> /history /load <id> /new /quit[/yellow]")


# ─── Typer commands ───────────────────────────────────────────────────────────

@app.command()
def main(
    message: Optional[str] = typer.Option(None, "--message", "-m", help="Single message (non-interactive)"),
    server: str = typer.Option(
        os.environ.get("AURORA_SERVER", "http://localhost:8000"),
        "--server", "-s", help="Aurora server URL",
    ),
    api_key: str = typer.Option(
        os.environ.get("AURORA_API_KEY", ""),
        "--api-key", "-k", help="API key",
    ),
    model: str = typer.Option(
        os.environ.get("AURORA_MODEL", ""),
        "--model", "--mod", help="Model ID, e.g. anthropic/claude-sonnet-4-6",
    ),
    conversation: Optional[str] = typer.Option(
        None, "--conversation", "-c", help="Load existing conversation ID",
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress tool/thinking output"),
    verbose: bool = typer.Option(True, "--verbose", "-v", help="Show tool calls and thinking"),
):
    """Aurora CLI — AI assistant."""
    client = Client(server=server, api_key=api_key, model=model)
    if conversation:
        client.conv_id = conversation

    async def run():
        if message:
            await client.chat(message, verbose=not quiet and verbose)
        else:
            await client.interactive(verbose=not quiet and verbose)

    asyncio.run(run())


if __name__ == "__main__":
    app()
