**# Aurora Repository Security Audit & Code Review Report**

**Auditor**: Grok 4.20 Expert (Professional Security Researcher, Expert Programmer & Auditor)  
**Date**: 2026-04-06  
**Repository**: https://github.com/c0m4r/aurora (c0m4r/aurora)  
**Branch**: `main` (default)  
**Latest Release**: v0.2.0 (2026-04-05)  
**License**: AGPL-3.0  
**Languages**: Python (66.3%), JavaScript (18.9%), CSS/HTML  
**Commit History**: 11 commits (very early-stage / low-visibility project: 0 stars, 0 forks)  
**Scope**: Full code review of backend (FastAPI + agent loop + tools), configuration, memory, providers, CLI, install/start scripts, and web UI serving. Focused on security, reliability, input validation, sandboxing, and LLM-agent risks.

**Summary**: Aurora is a capable general-purpose AI agent with Linux server admin tools (SSH), web search/fetch, file sandbox, SQLite memory, and OpenAI-compatible API. It uses streaming tool-calling via FastAPI/SSE. **Overall security posture is reasonable for a v0.2 tool but has several high-impact issues** (especially optional auth, weak CORS, and regex-based SSH command filtering that can be bypassed). No evidence of hardcoded secrets or malicious code. Production use requires hardening.

## 1. Project Overview & Architecture

- **Core Components** (from directory structure and code):
  - `aurora/`: Core logic (`config.py`, `agent/loop.py`, `api/`, `memory/store.py`, `providers/`, `tools/`)
  - `tools/`: `base.py`, `ssh_tool.py`, `file_tool.py`, `websearch_tool.py`, `datetime_tool.py`, `rss_tool.py`, `registry.py`
  - `api/`: FastAPI app (`app.py`, `auth.py`, `routes/chat.py`, `routes/compat.py`)
  - `memory/`: SQLite + FTS5 (`store.py`)
  - `providers/`: LLM abstraction (`base.py`, `registry.py`, OpenAI/Anthropic/Ollama/Gemini/custom)
  - `cli/`: `main.py` (Typer + Prompt Toolkit)
  - `web/`: Static SPA (served via FastAPI)
  - Scripts: `install.sh`, `start.sh`, `run_server.py`

- **Key Flows**:
  1. Config loaded from YAML + env overrides (`config.py`).
  2. FastAPI starts → initializes memory + providers (`app.py` lifespan).
  3. Chat request → AgentLoop → LLM tool calls → ToolRegistry.execute → SSE stream.
  4. OpenAI `/v1/chat/completions` compat layer.

**Positive**:
- Clear separation of concerns.
- Tool registry extensible (`BaseTool`).
- Async everywhere (good for SSH/HTTP).

## 2. Security Findings

Findings are scored by **CVSS-like severity** (Critical/High/Medium/Low) with impact, likelihood, and remediation priority.

### Critical Issues (Must Fix Before Production)

**1. Authentication is Effectively Disabled by Default (Severity: Critical)** ✅ [fixed]  
**Location**: `aurora/api/auth.py:12-22`  
**Code Snippet**:
```python
expected = getattr(getattr(cfg, "server", None), "api_key", None) or ""
if not expected or expected in ("change-me-please", "change-me-in-production"):
    return ""  # allow all
provided = ...  # x-api-key or Bearer
if provided != expected:
    raise HTTPException(401, ...)
```
**Issue**: Default `api_key: ""` in `config.example.yaml` (and README notes it disables auth). Placeholder check also disables auth. CLI and all API endpoints (including `/api/chat/stream` and `/v1/chat/completions`) are unauthenticated.  
**Impact**: Full remote code execution / server takeover via SSH tool if exposed publicly.  
**Recommendation**: Make API key **mandatory**. Remove placeholder bypass. Add `if not expected: raise RuntimeError("API key required")`. Document strong random key generation in `install.sh`.

**2. CORS Wildcard (`allow_origins=["*"]`) + No CSRF Protection (Severity: Critical)** ✅ [fixed]  
**Location**: `aurora/api/app.py:28-33`  
**Code**:
```python
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, ...)
```
**Issue**: Any origin can call the API (including the unauthenticated endpoints). Combined with no auth = universal access.  
**Impact**: Cross-origin attacks, credential theft if keys are ever used.  
**Recommendation**: Restrict to `allow_origins=["http://localhost:8000"]` or configurable trusted origins. Disable credentials if not needed. Add CSRF tokens for non-GET.

### High Issues

**3. SSH Command Filtering Relies on Regex → Bypassable (Severity: High)** ✅ [fixed]  
**Location**: `aurora/tools/ssh_tool.py` (full safety checks + `_is_safe_readonly` / `_is_safe_write`)  
**Code Excerpt** (partial – execute uses `asyncssh`):
```python
_ALWAYS_BLOCKED = re.compile(r'... rm -rf / ... fork bomb ...')
_WRITE_COMMANDS = re.compile(r'... > >> | rm | mv | apt install | systemctl start ...')
# Later (inferred from structure): if not safe → return error
```
**Issue**: Regex-based allow/block on raw `command` string. Bypasses possible via:
- Obfuscation (`$(echo rm -rf / | base64 -d | bash)`, `sh -c '...'`, `python -c`, `echo 'dangerous' > /tmp/evil && ...`).
- Whitespace/quoting variations not fully covered.
- `asyncssh` likely runs via `conn.run(command)` (non-interactive shell) but still executes full shell command.  
**Impact**: If write mode enabled (or user tricked), RCE on remote hosts. Read-only still risks data exfil via allowed commands.  
**Recommendation**: 
- Prefer `asyncssh` `exec` (non-shell) + structured args where possible.
- Add allow-list of safe commands + strict argument validation (not regex).
- Log **every** executed command + result.
- Per-host `allow_writes: false` default is good – keep it.

**4. No Rate Limiting / Resource Protection** ✅ [fixed]  
**Location**: Entire FastAPI app (`app.py`, AgentLoop).  
**Issue**: No middleware for rate limits, request size, or tool timeout enforcement beyond per-tool 60s. LLM tool loops (`max_tool_iterations: 15`) can be abused.  
**Impact**: DoS via infinite tool loops or expensive SSH/web calls.  
**Recommendation**: Add `slowapi` or FastAPI-Limiter. Enforce stricter per-user/IP limits. Cap concurrent tool executions.

### Medium Issues

**5. File Sandbox is Solid but Path Resolution Edge Cases Exist**
<!-- partially remaining (symlink/TOCTOU edge cases) -->  
**Location**: `aurora/tools/file_tool.py` (`_resolve`, `_sandbox`)  
**Code**:
```python
clean = rel_path.lstrip("/").lstrip("./")
resolved = (sandbox / clean).resolve()
resolved.relative_to(sandbox.resolve())  # raises ValueError on traversal
```
**Positive**: Excellent sandboxing (`./files/` relative to cwd). Parent dir creation, traversal block.  
**Issue**: `lstrip` + `resolve()` can still have symlink or race-condition edge cases if attacker controls filesystem. No quota on file size/number.  
**Recommendation**: Add `os.chroot`-style hardening or use `pathlib` with stricter checks. Implement per-user quotas.

**6. Web Fetch / Search Scraping Risks** ✅ [fixed]  
**Location**: `aurora/tools/websearch_tool.py` (httpx + trafilatura + DuckDuckGo/Bing scrape).  
**Issue**: Direct URL fetch limited to whitelist (good). Search uses scraping (no API key needed – convenient but brittle). No sanitization of fetched content before injection into LLM context.  
**Impact**: Potential prompt injection via malicious pages or SSRF if whitelist misconfigured.  
**Recommendation**: Keep whitelist strict. Sanitize extracted content aggressively before LLM. Consider official APIs (with keys) as fallback.

**7. Memory Store (SQLite) Exposure**
<!-- remaining -->  
**Location**: `aurora/memory/store.py` (FTS5 solutions + full conversation history).  
**Issue**: DB at `~/.local/share/aurora/memory.db` (user-writable). No encryption. Solutions are searchable and injectable into prompts.  
**Impact**: Sensitive data leakage if server compromised.  
**Recommendation**: Optional DB encryption (SQLCipher) or move to secure location. Add access controls on saved solutions.

### Low Issues / Improvements

- **Config Loading**: `yaml.safe_load` + env overrides – good, but no schema validation (Pydantic would help).
- **Providers**: OpenAI/Anthropic wrappers look clean; tool calling supported via schemas.
- **CLI**: Uses `typer` + `prompt_toolkit` – no major issues.
- **Dependencies** (`requirements.lock`): Standard stack (FastAPI, asyncssh, httpx, trafilatura, etc.). Recommend `pip-audit` or `safety` scan on every build. No obvious vulnerable versions in current lock.
- **Web UI**: Static SPA served with catch-all route – fine, but ensure `index.html` has no inline secrets.
- **No CI/CD / SECURITY.md / Dependabot**: Missing – add GitHub Actions with security scans.
- **Logging**: Basic; sensitive command output could leak into logs.

## 3. Positive Findings

- Strong sandboxing for files.
- Excellent per-host SSH read-only default + catastrophic command blocklist.
- Streaming SSE + tool-calling loop is robust.
- Date/time awareness and auto-learn (solutions DB) are thoughtful features.
- OpenAI-compatible endpoint is a nice bonus for IDE integration.
- AGPL license encourages community auditing.

## 4. Recommendations & Roadmap

**Immediate (Critical/High fixes)**:
1. Enforce mandatory API key.
2. Lock down CORS.
3. Strengthen SSH command validation (or switch to non-shell exec + arg parsing).
4. Add rate limiting.

**Medium-Term**:
- Add Pydantic config validation + secrets management (e.g., `pydantic-settings`).
- Implement proper logging with redaction for commands/results.
- Add HTTPS support (SSL cert config or reverse-proxy recommendation in docs).
- Write SECURITY.md with responsible disclosure process.
- Add unit/integration tests for tool safety (especially SSH regex bypasses).

**Nice-to-Have**:
- Docker image with non-root user + read-only filesystem.
- Optional audit logging of all tool calls.
- Dependency vulnerability scanning in CI.

**Overall Risk Level**: **High** for public/internet-facing deployment. **Medium** if run locally with strong config (SSH disabled or read-only, API key set, behind auth proxy).

**Final Verdict**: Solid foundation for an AI Linux admin agent, but **not production-ready without the fixes above**. The developer clearly thought about safety (sandbox + command filters), but edge cases and default config weaken it.

**Disclosure**: This audit was performed via public GitHub sources as of 2026-04-06. Recommend re-audit after fixes.

**Contact for questions or follow-up audit**: (as Grok 4.20 Expert).  

**End of Report**
