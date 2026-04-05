<div align="center">

# 🪼 Aurora

A general-purpose AI assistant with Linux server administration capabilities.

Ask questions, diagnose problems, run commands on remote servers, search the web, and save files — all through a streaming chat interface backed by any LLM.

<img width="866" height="674" alt="image" src="https://github.com/user-attachments/assets/d26449b0-f177-4e79-b800-c12dc731a0be" />

</div>

## Features

- **SSH access** — run commands on remote Linux servers; read-only by default, write mode opt-in per host
- **Web search & fetch** — DuckDuckGo search (no API key) + direct fetching from a whitelisted domain list
- **File storage** — read and write files in a local `./files/` sandbox
- **Date/time awareness** — always knows the current time and can build precise time-range queries
- **Persistent memory** — SQLite + FTS5 stores conversations and saved solutions; relevant past solutions are injected into context automatically
- **Model agnostic** — Ollama, Anthropic Claude, OpenAI, Gemini or any OpenAI-compatible endpoint; switch per-conversation
- **Web UI** — dark/light mode, collapsible tool call panels, token usage, conversation history
- **CLI client** — rich terminal UI, connects to a remote server so every team member can use it
- **OpenAI-compatible `/v1` API** — connect opencode, Cursor, or any OpenAI-protocol client

---

## Quick Start

```bash
git clone https://github.com/c0m4r/aurora.git
cd aurora
./install.sh
```

The installer creates a virtual environment, installs dependencies, and copies `config.example.yaml` → `config.yaml` if it doesn't exist.

Start the server with:

```bash
./start.sh
```

Open **http://localhost:8000**.

---

## Configuration

Everything lives in `config.yaml`. Environment variables override the corresponding keys.

### Model Providers

Enable at least one. Models are referenced as `provider/model-id`.

Ollama provider is enabled by default.

### SSH

Connect to Linux servers and run shell commands.

```yaml
tools:
  ssh:
    enabled: true
    allow_writes: false   # true = allow state-changing commands when user asks
    hosts:
      - name: "web-01"
        host: "10.0.0.10"
        port: 22
        user: "aurora"
        key_file: "~/.ssh/id_ed25519"
        # allow_writes: true   # per-host override
      - name: "db-01"
        host: "10.0.0.20"
        user: "root"
        key_file: "~/.ssh/id_ed25519"
```

**Safety model:**

| Mode | What's blocked |
|---|---|
| Read-only (default) | Any write/modify operation: redirects (`>`), package managers, `systemctl start/stop/restart`, `rm`, `chmod`, `useradd`, `mount`, `kill`, `reboot`, and ~40 more patterns |
| Write (`allow_writes: true`) | Only catastrophic/irreversible operations: `rm -rf /`, `mkfs`, `dd` to block devices, fork bombs |

The model is instructed to only use write commands when the user has explicitly asked for a change, and to announce what each command will do before running it.

### Web Search & Fetch

Searches DuckDuckGo (falls back to Bing). No API key required. Uses `trafilatura` for content extraction when available.

```yaml
tools:
  websearch:
    enabled: true
    max_results: 5
    fetch_content: true        # extract page text from top results
    max_content_length: 4000   # characters per page

    # Domains the model may visit directly with a URL (without a search query).
    # null = use built-in defaults (GitHub, PyPI, Arch wiki, NVD, Stack Exchange, …)
    # []   = disable direct URL fetching
    whitelist: null
    # whitelist:
    #   - github.com
    #   - wiki.archlinux.org
    #   - your-internal-docs.example.com
```

### File Storage

Always enabled. The model can read and write files inside `./files/` (relative to the server's working directory). Path traversal is blocked — nothing outside that directory is accessible.

```
./files/
  report.md
  scripts/setup.sh
  data/output.json
```

### Memory

```yaml
memory:
  db_path: "~/.local/share/aurora/memory.db"
```

Conversations and saved solutions are stored in SQLite with FTS5 full-text search. Relevant past solutions are automatically injected into the system prompt for each new query. Save a solution via the web UI's **📚 Solutions** panel.

### Server

```yaml
server:
  host: "0.0.0.0"
  port: 8000
  api_key: "strong-random-secret"   # or: AURORA_API_KEY env var
```

If `api_key` is empty or `change-me-please`, authentication is disabled (fine for local use).

---

## Web UI

Open **http://localhost:8000** after starting the server.

| Feature | Details |
|---|---|
| Streaming | Responses stream token-by-token via SSE |
| Stop | Red ■ button (or Esc) aborts the current generation; partial response is kept |
| Continue | Button appears automatically when the agent hits max tool iterations |
| Thinking | Claude's extended reasoning in a collapsible block |
| Tool calls | Each tool invocation shows input + output, collapsible |
| Token usage | Per-message and session-total token counts |
| Dark / light | Toggle in the sidebar footer |
| Copy | Per-message copy button; copy entire conversation; copy code blocks |
| History | All conversations saved and listed in the sidebar |
| Solutions | Saved solutions panel (📚) — browse, re-ask, delete |
| Settings | Right-click the ⚡ logo to set server URL and API key |

---

## CLI

Thin client that connects to any running server — install it on any machine.

```bash
pip install -e .
# or run directly without installing:
python cli/main.py
```

**Interactive:**
```bash
aurora
aurora --server http://server:8000 --api-key my-secret
```

**Single shot:**
```bash
aurora -m "check disk and memory on all servers"
aurora -m "what is the latest kernel version?" --quiet
```

**Environment variables:**
```bash
export AURORA_SERVER=http://server:8000
export AURORA_API_KEY=my-secret
export AURORA_MODEL=anthropic/claude-sonnet-4-6
aurora
```

**In-session commands:**

| Command | Description |
|---|---|
| `/models` | List all available models from all providers |
| `/use anthropic/claude-opus-4-6` | Switch model for this session |
| `/new` | Start a new conversation |
| `/history` | List recent conversations |
| `/load <id-prefix>` | Resume a past conversation |
| `/quit` | Exit |

---

## OpenAI-Compatible API

The server exposes `/v1/chat/completions` in the OpenAI format, so any tool that accepts a custom base URL works out of the box.

**opencode:**
```json
{
  "providers": {
    "agent": {
      "api": "openai",
      "base": "http://localhost:8000/v1",
      "key": "your-api-key"
    }
  }
}
```

**Cursor / Continue / VS Code:**
- Base URL: `http://localhost:8000/v1`
- API Key: value of `api_key` in `config.yaml`

**Python:**
```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="your-key")
stream = client.chat.completions.create(
    model="anthropic/claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Check nginx status on web-01"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

---

## Architecture

```
┌─────────────┐   SSE stream    ┌──────────────────────────────────────┐
│  Web UI     │◄───────────────►│                                      │
├─────────────┤                 │           FastAPI Server             │
│  CLI client │◄── SSE stream ─►│  /api/chat/stream  (native SSE)      │
├─────────────┤                 │  /v1/chat/completions  (OpenAI compat)│
│  opencode   │◄── OpenAI API ─►│                                      │
└─────────────┘                 └──────────────┬───────────────────────┘
                                               │
                                      ┌────────▼────────┐
                                      │   Agent Loop    │  async generator
                                      │  (loop.py)      │  → SSE events
                                      └────────┬────────┘
                                               │
                              ┌────────────────┼────────────────┐
                              │                │                │
                     ┌────────▼──┐    ┌────────▼────────┐  ┌───▼──────────┐
                     │ Provider  │    │  Tool Registry  │  │   Memory     │
                     │ Registry  │    │                 │  │  (SQLite)    │
                     └─────┬─────┘    │  ssh            │  └──────────────┘
                           │          │  web (search +  │
              ┌────────────┤          │    whitelisted  │
              │ Anthropic  │          │    fetch)       │
              │ OpenAI     │          │  file_read      │
              │ Gemini     │          │  file_write     │
              │ Ollama     │          │  get_datetime   │
              │ Custom     │          └─────────────────┘
              └────────────┘
```

**SSE event stream** (all clients receive the same format):

| Event | Description |
|---|---|
| `conv_id` | Conversation ID (first event on new conversation) |
| `thinking` | Claude extended reasoning delta |
| `text` | Response text delta |
| `tool_call` | Tool name + input being invoked |
| `tool_result` | Tool output (or error flag) |
| `usage` | Input / output token counts |
| `done` | Turn complete |
| `error` | Unrecoverable error |

---

## Adding Tools

1. Create `aurora/tools/my_tool.py`:

```python
from .base import BaseTool, ToolDefinition

class MyTool(BaseTool):
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="my_tool",
            description="What this tool does and when to use it.",
            parameters={
                "type": "object",
                "properties": {
                    "input": {"type": "string", "description": "..."},
                },
                "required": ["input"],
            },
        )

    async def execute(self, input: str, **_) -> str:
        return "result"
```

2. Register it in `aurora/tools/registry.py` → `build_registry()`.

---

## Dependencies

Overview:

| Package | Purpose |
|---|---|
| `fastapi` + `uvicorn` | Async HTTP server |
| `anthropic` | Claude API (native streaming + extended thinking) |
| `openai` | OpenAI / Gemini / Ollama / vLLM compatible APIs |
| `aiosqlite` | Async SQLite for conversation history and memory |
| `asyncssh` | SSH connections to remote servers |
| `httpx` | Async HTTP (web search, URL fetching) |
| `beautifulsoup4` + `lxml` | HTML parsing for web search and page extraction |
| `trafilatura` | Better main-content extraction from web pages |
| `rich` + `typer` + `prompt_toolkit` | CLI |
| `pyyaml` | Config file parsing |

Full list: [requirements.lock](requirements.lock)

---

## License

See [LICENSE](LICENSE) file.
