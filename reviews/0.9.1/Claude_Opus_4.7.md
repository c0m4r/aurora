# 🪼 Aurora — Security & Code-Quality Review

**Version reviewed:** 0.9.0 (commit aa5d27c)  
**Reviewer perspective:** professional security researcher / senior Python engineer  
**Scope:** whole repository (server, tools, web UI, providers, CLI, installer)

---

## 1. Executive Summary

Aurora is a well-structured FastAPI agent server that gives an LLM SSH / file / web / RSS capabilities. The code is readable, the security posture is clearly thought-through (SSRF guard, CSP, rate limiting, sandbox, auth fail-closed, DOMPurify, defusedxml, approval mode), and it is miles ahead of the typical hobby "agent framework".

That said, the threat model is extremely broad (arbitrary code execution on a remote root-SSH'd host is literally a first-class feature), so small regressions have catastrophic consequences. The most serious issues cluster around the SSH safety model — a blacklist-based shell-command filter, which is the classical wrong tool for the job — and a handful of smaller sandbox / SSRF / input-validation gaps.

### Overall score

| Dimension | Score |
|---|---|
| Architecture & clarity | 8.5 / 10 |
| Code quality | 8 / 10 |
| Performance | 7.5 / 10 |
| Security (defensive design) | 6.5 / 10 |
| Security (attack-surface hardening) | 5 / 10 |
| **Composite** | **7 / 10** |

### Severity roll-up

| Severity | Count |
|---|---|
| 🔴 Critical | 1 |
| 🟠 High | 4 |
| 🟡 Medium | 9 |
| 🟢 Low | 8 |
| ℹ️ Informational / style | 6 |

---

## 2. Threat Model (for context)

Anyone with a valid API key (or an active OTP) can drive the agent, and the agent can:

- Run arbitrary commands over SSH as configured users (root on 10.0.0.1 in the shipped `config.yaml`)
- Read/write the local `./files/` sandbox
- Upload anything under `./files/` to any absolute path on a configured host
- Fetch arbitrary whitelisted URLs (and perform searches that auto-fetch non-whitelisted URLs from results)
- Persist arbitrary content into SQLite (solutions, conversations)

The LLM is effectively a semi-trusted confused deputy that processes attacker-controlled data (web pages, RSS, SSH output, tool outputs) and can be prompt-injected. This makes sandbox enforcement at the tool layer critical — you cannot rely on the model "behaving".

---

## 3. Findings

### 🔴 CRITICAL

#### C-1 — SSH command safety relies on a blacklist regex; the blacklist is bypassable

**Files:** `aurora/tools/ssh_tool.py:26-163`

`_EVASION_PATTERNS`, `_ALWAYS_BLOCKED`, and `_WRITE_COMMANDS` are a denylist. Denylists for shell commands are a fundamentally losing game; the code acknowledges this only tacitly. Real bypass classes the current regex misses:

**Redirect exclusion is too narrow** — `ssh_tool.py:99`

```
(?<![<2])\s?>(?!=)
```

Only excludes `2>`. `1>file`, `0>file`, `3>file`, or fd-duplication like `&>file` are all writes that slip past. Example: `cat secret 1> /tmp/x` is a write that is not blocked.

**Weird negative lookahead on plain `rm`** — `ssh_tool.py:105`

```
| \brm\b.*\s(?!.*\becho\b) # plain rm
```

Any command where the substring "echo" appears anywhere downstream suppresses the match. `rm /tmp/secret # echo` (a trailing comment) is allowed in read-only mode.

**No coverage for common destructive binaries** — `install` (GNU coreutils — copies+chmods), `cp --force`, `rsync --delete`, `sed -i`, `perl -pi -e`, `awk -i inplace`, `find ... -delete`, `update-*` helpers, `systemd-run`, `loginctl terminate-user`, `swapoff`, `fallocate`, `truncate` is blocked but not `fallocate`, `logger -p kern.emerg`, `wall`, etc.

**Evasion patterns are incomplete:**

- Missing: `command -p`, `builtin eval`, `env -i`, `nsenter`, `unshare`, `podman/docker exec`, `nc -e`, `socat EXEC:`, `screen -dmS`, `tmux new -d`, `at now`, `systemd-run --scope`, `rlwrap`, `script -c`.
- `\benv\s+(?:[A-Z_][A-Z0-9_]*=\S+\s+)*(?:ba|da|z|tc|k|c)?sh\b` requires var names in uppercase — `env foo=bar bash` (lowercase) bypasses.

**Unicode & multiline gaps** — NFKC normalisation is good, but bash supports `$'\n'` literal newlines in `$'…'`, `\<NL>` line-continuations, and IFS tricks (`${IFS}`). None of these are accounted for.

**Interpreter whitelisting missing** — `node -e`, `deno eval`, `bc <<<`, `gdb -batch -ex`, `ex -c`, `vim -E -c`, `ed`, `jq --arg`, `sqlite3 :memory:`, and dozens more can run shell / filesystem operations.

**Impact:** in read-only mode an attacker-driven model can still modify/exfiltrate state; in write mode with `allow_writes: true` + root user (as in the shipped `config.yaml`), anything not caught is a full root compromise on the target host.

**Recommendation:** treat the blacklist as advisory telemetry, not a security boundary. Real mitigations:

```python
# 1. Switch to an allow-list of shell commands in read-only mode
_READONLY_ALLOWLIST = {
    "ls", "cat", "ss", "ps", "top", "free", "df", "du", "uname", "uptime",
    "journalctl", "dmesg", "systemctl", "ip", "ping", "dig", "host",
    "tail", "head", "grep", "awk", "sed", "find", "stat", "readlink",
    # …explicit list
}
# 2. Parse with shlex and check only argv[0] against allowlist; forbid pipes/redirects entirely
# 3. Instead of passing a shell string, invoke the binary directly via asyncssh:
#      await conn.run(cmd_argv, input=None, check=False, stdin=None, stdout=..., stderr=...)
# 4. For write mode: still force it through a curated list plus an explicit "--yes-i-mean-it" config flag,
#    and keep catastrophic filters as a second layer.
```

If a real allow-list is too restrictive for your use-case, at minimum:

- Reject any redirect in read-only mode (`[<>]` anywhere) rather than trying to exclude the `2>` case.
- Fix the `rm` rule to not depend on the magic word "echo".
- Log every blocked command as a security event so bypass attempts are visible.
- Mark this as "best-effort guardrail, not a security boundary" in the README and force the operator to enable per-host `allow_writes` only for non-privileged users.

---

### 🟠 HIGH

#### H-1 — The shipped `config.yaml` is dangerous: unauthenticated + writes + root SSH

**File:** `config.yaml:1-88`

```yaml
allow_unauthenticated: true
...
ssh:
  enabled: true
  allow_writes: true
  hosts:
    - host: "10.0.0.1"
      user: "root"
      key_file: "~/.ssh/id_ed25519"
```

Any process on the machine (including a curious browser extension / dev server / malicious npm script / local user) can `curl http://127.0.0.1:8000/api/chat/stream` and instruct the LLM to ssh … `root@10.0.0.1` … with full write permission.

`validate_auth_config()` correctly refuses `allow_unauthenticated=true` on a non-loopback bind, but loopback is not a real isolation boundary on a multi-user / multi-process box.

**Recommendation:**

- Ship `config.example.yaml` (done) and exclude `config.yaml` from the repo (it's already there — suggest adding it to `.gitignore` to prevent developers committing production-like configs). Your `.gitignore` should be reviewed.
- Refuse `allow_unauthenticated=true` combined with any enabled tool that has `allow_writes=true` or `enable_openai_compat=true`.
- Default new configs to `allow_writes: false` at the per-host level; require opt-in per host, not per tool.
- Refuse to start if an SSH host's user is `root` and `allow_writes` is `true` and auth is disabled.

```python
# auth.py — add to validate_auth_config
if allow_unauth:
    if _any_host_has_writes(cfg):
        raise RuntimeError("allow_unauthenticated=true with SSH allow_writes=true is refused.")
```

#### H-2 — DNS rebinding TOCTOU in `safe_httpx_client`

**File:** `aurora/tools/_http_guards.py:83-164`

`validate_url()` resolves the hostname, checks IPs, then lets httpx re-resolve and connect. An attacker controlling DNS can answer public IP at check time and loopback/metadata IP at connect time. The docstring even admits this.

**Recommendation:** resolve once, then connect by IP with an explicit `Host:` header (`httpx: client.get(f"https://{ip}/...", headers={"Host": hostname})` plus SNI wiring). Or use `httpx.AsyncHTTPTransport(local_address=...)` with a custom `AsyncResolver` that caches the validated answer for the life of the request.

```python
# Minimal patch: pin to the first validated address, connect via IP + Host header.
# httpx: transport = httpx.AsyncHTTPTransport(retries=0)
# Then patch ._request to build the URL with the literal IP and pass Host header.
```

#### H-3 — `weather_tool.py` uses `httpx` directly with redirects, bypassing SSRF guards

**File:** `aurora/tools/weather_tool.py:214-221`

```python
async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
    resp = await client.get(f"{_OPEN_METEO_BASE}/forecast", params=params)
```

If `api.open-meteo.com` is hijacked/proxied/redirects to a private address, the weather tool will dutifully follow. Every other fetch in the codebase uses `safe_httpx_client` — this is the sole outlier.

**Recommendation:**

```python
from ._http_guards import safe_httpx_client
async with safe_httpx_client(timeout=15.0) as c:
    resp = await c.get(f"{_OPEN_METEO_BASE}/forecast", params=params)
```

#### H-4 — OTP & configured API key are logged to stdout and cross-boundary

**Files:** `aurora/api/auth.py:18-21`, `aurora/api/app.py:283-291`, `aurora/api/app.py:183-191`

- At startup the OTP is `print()`ed in plain text. Anyone who can read the process's terminal/log/journalctl output captures it.
- `POST /api/login` with a valid key or OTP returns the configured long-lived `api_key` in the JSON body. That means: OTP ⇒ permanent key. Anyone who briefly sees the OTP gets perpetual access until config is rotated.
- OTP lifetime = server lifetime; it's never rotated.

**Recommendation:**

- Don't return `api_key` from `/api/login`. Issue a short-lived bearer token (signed) or a server-side session cookie with `HttpOnly; SameSite=Strict`.
- Rotate the OTP on each successful login and expire it after N minutes; invalidate after first use.
- Log the OTP only to a stderr-only message behind a flag, and scrub it from uvicorn's access log.
- Consider a minimum API key length (currently any non-sentinel string passes).

---

### 🟡 MEDIUM

#### M-1 — OpenAI-compatible `/v1` route bypasses session file-isolation

**File:** `aurora/api/routes/compat.py:99-105`

```python
loop = AgentLoop(registry, tools_reg, cfg)   # no conversation_id
...
async for event in loop.run(messages, model_id, **kwargs):
```

`set_session(None)` → file tools use the global `./files/` root, shared with every user of the compat endpoint. Two `opencode` users hitting the same server can read/overwrite each other's files.

Also: `extra_system` is put into `kwargs` and forwarded to `loop.run(..., **kwargs)`, which forwards it to `provider.stream()` — but `_build_system()` never consumes it, so a client-supplied system prompt is silently dropped on Anthropic/OpenAI providers.

**Recommendation:** generate a per-call / per-client session ID; accept a session header or derive it from a hash of `Authorization` to get per-key isolation. Thread the system prompt properly (merge with built-in system, don't replace it).

#### M-2 — Rate limiter trusts `X-Forwarded-For` unconditionally + grows unbounded

**File:** `aurora/api/app.py:58-109`

```python
forwarded = request.headers.get("X-Forwarded-For")
if forwarded:
    return forwarded.split(",")[0].strip()
```

When the server is bound to `127.0.0.1` with no trusted proxy, an unauthenticated caller can spoof `X-Forwarded-For` to an arbitrary value per request → trivial rate-limit bypass (and, on the same request, DoS via dict growth). `self._windows` is never pruned: an attacker can insert millions of entries to exhaust memory (a single 16-byte IP key + deque ~200 B). `request.client.host` is used as the fallback — fine — but there is no "trust proxies" config.

**Recommendation:**

```python
TRUST_PROXY = getattr(cfg.server, "trust_proxy", False)
def _client_ip(self, request):
    if TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

# plus: evict empty deques periodically
if not dq:
    del self._windows[key]
```

#### M-3 — SCP upload allows clobbering any path on the remote host

**File:** `aurora/tools/scp_upload_tool.py:95-113`

`destination` is passed straight to `sftp.put(...)`. Combined with a key-file-authenticated SSH session as root, the model can overwrite `/etc/passwd`, `/root/.ssh/authorized_keys`, `/etc/sudoers.d/*`, `/lib/systemd/system/*.service`, etc., with arbitrary content written to `./files/` via `file_write`.

**Recommendation:**

- Gate SCP behind a per-host `allow_upload_paths: ["/tmp/aurora/*", "/home/aurora/uploads/*"]` allow-list and match via `pathlib.PurePosixPath`.
- Refuse absolute paths unless the host was configured with `allow_absolute_upload: true`.
- Log every upload with the source hash.

#### M-4 — Tool-call approval is not scoped to the caller

**File:** `aurora/api/routes/chat.py:499-509`, `aurora/agent/loop.py:22-37`

If more than one client shares the API key (very common in practice), any one of them can `POST /api/tool_approve` to approve another user's pending call. The only scoping is `conversation_id`, which is visible in the SSE stream (and is a UUID — predictable enough when you can guess from conversation listings).

**Recommendation:** bind pending approvals to the authenticated principal (derive a session id from a server-side login token) or at minimum require the approver to supply a one-time token emitted inside the same SSE stream as `tool_approval_required`.

#### M-5 — Path-resolution has a race between resolve and write

**File:** `aurora/tools/sandbox.py:57-116`

`resolve()` calls `.resolve()` (follows symlinks, checks containment), then returns the resolved path. Between that call and the subsequent `open()` / `write_text()`, the sandbox directory itself could be swapped for a symlink (TOCTOU). Less of an issue because only the model can plant symlinks, and the code never creates symlinks. Still: if ever a future tool does, the attack becomes live.

**Recommendation:**

- Open files with `os.open(path, os.O_NOFOLLOW | os.O_CREAT | os.O_WRONLY, 0o600)` and enforce the directory via `openat` semantics (Python 3.11+: `os.open(..., dir_fd=sandbox_fd)`).
- Reject any write where `Path(target).parent.resolve()` doesn't equal the expected parent.

#### M-6 — No size/rate cap on `file_write`

**File:** `aurora/tools/file_tool.py:127-164`

```python
with open(target, mode, encoding="utf-8") as fh:
    fh.write(content)
```

A prompt-injected model can exhaust local disk by writing several GB into `./files/sessions/<cid>/bomb`. There is a 20 KB preview cap returned to the model, but the file itself is unbounded.

**Recommendation:**

```python
MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MB per write
if len(content.encode("utf-8")) > MAX_FILE_BYTES:
    return f"[BLOCKED] content exceeds {MAX_FILE_BYTES} bytes"
# plus: enforce a sandbox-wide quota via shutil.disk_usage checks
```

#### M-7 — `run_extract()` prompt-injection filter is string-match

**File:** `aurora/agent/learner.py:146-161`

```python
_INJECTION_RE = re.compile(
    r"SYSTEM\s*:|###\s*system\b|ignore\s+(?:previous|prior|above)\s+instructions"
    r"|<\s*script\b|<\s*iframe\b|javascript\s*:",
    re.IGNORECASE,
)
```

Dozens of obvious bypasses: different language ("disregarde les instructions", "forget what I said before"), Unicode homoglyphs, zero-width joiners, base64-wrapped instructions, "role: system", etc. Auto-learn is off by default and excluded for external tool outputs, which is the real mitigation — but the filter creates a false sense of defense-in-depth.

**Recommendation:** drop the regex or mark it explicitly as "best-effort formatting hygiene"; rely on the tool-class exclusion (already present) as the real control. Consider structured output validation (JSON Schema) instead of accepting arbitrary "problem"/"solution" strings.

#### M-8 — `/api/conversations/{cid}/files/content` reads up to 2 MB with no auth per conversation

**File:** `aurora/api/routes/chat.py:427-456`

Any authenticated user can read any session's files by guessing the UUID. UUID4 is unguessable in practice, but the endpoint `/api/conversations` returns all conversation IDs — so authenticated users see everyone's conversations and can read all files.

**Recommendation:** if multi-user, add an `owner` column to conversations (derive from the authenticated principal) and filter list/get by owner.

#### M-9 — `file_read` error messages leak path + exception text

**File:** `aurora/tools/file_tool.py:69-84`

```python
except Exception as exc:
    return f"Error reading files/{path}: {exc}"
```

Low-severity info leak back to the model (which may surface it to an attacker). Same pattern in `file_edit_tool.py`, `scp_upload_tool.py`, `ssh_tool.py`. For external errors, log and return a generic message.

---

### 🟢 LOW

**L-1 — Progress queue is unbounded**  
`aurora/agent/loop.py:299-334` — `asyncio.Queue()` with no `maxsize`. A chatty SSH tool could swell memory before the consumer drains it. Add `maxsize=1024` with a "drop oldest" policy or back-pressure.

**L-2 — Static mount serves the entire `web/` directory**  
`aurora/api/app.py:210` — `StaticFiles(directory=str(WEB_DIR))` is mounted at `/assets`, so anything added to `web/` later becomes public. Consider mounting only `web/css` and `web/js`.

**L-3 — CSP allows `'unsafe-inline'` for styles**  
`aurora/api/app.py:30`. You already use `data-action` delegation for scripts — good. Inline styles remain in `app.js` (e.g. `errEl.style.color = 'var(--red)'`). You could either use CSS classes (remove `'unsafe-inline'`) or accept this as a known trade-off. Low risk since DOMPurify strips style attrs from LLM content, but an unsanitised path (e.g., a new feature forgetting `safeMarkdown`) would become an XSS vector.

**L-4 — `marked.parse` is applied before DOMPurify**  
`web/js/app.js:66-78`. Works today because DOMPurify runs afterwards, but `marked` can emit `javascript:` URIs in links depending on version. Since you use `DOMPurify.sanitize(raw, {USE_PROFILES:{html:true,svg:true}})`, `javascript:` is stripped — but consider `DOMPurify.sanitize(..., {FORBID_ATTR: ['formaction', 'action', 'xlink:href']})`.

**L-5 — `_CITIES` partial matching is surprising**  
`aurora/tools/weather_tool.py:229-233`. `"beijing" in "berlin"` is False, but `"ber" in "berlin"` returns Berlin. Not a security issue; just produces wrong answers for "bern" (returns "berlin").

**L-6 — `ssh_tool` trusts integer timeout**  
`aurora/tools/ssh_tool.py:301` — `min(int(timeout or 60), 300)` — if the model passes a string like `"nan"` or `"-1"`, `int("nan")` raises `ValueError` which bubbles to the caller. Catch and default:

```python
try:
    timeout = min(max(int(timeout or 60), 1), 300)
except (TypeError, ValueError):
    timeout = 60
```

**L-7 — UUID validation accepts any UUID variant**  
`aurora/api/routes/chat.py:28-33` — `UUID(cid)` accepts v1 (time-MAC based) and v2, which leak host MAC. Since `create_conversation()` uses `uuid.uuid4()`, restrict to v4:

```python
u = _uuid_mod.UUID(cid)
if u.version != 4:
    raise HTTPException(400, "Invalid conversation ID")
```

**L-8 — `MemoryStore` opens a new connection per call**  
`aurora/memory/store.py` — every `async with aiosqlite.connect(...)` pays fork/fsync cost. On a busy machine this matters. Use a single pooled connection in the lifespan startup.

---

### ℹ️ Informational / Style

- **I-1** `aurora/config.py` uses `_Obj` hierarchical wrapper — works, but using `pydantic-settings` (already in lockfile) would give typed config with validation for free.
- **I-2** `FeedCatalogue` hard-codes URLs — making it configurable is easy and avoids code edits for new feeds.
- **I-3** `pip_check.py` is a nice tool but duplicates `pip list --outdated` + PyPI fetching; consider `pip-audit` (adds CVE info).
- **I-4** `aurora/api/app.py:283` — ANSI colour printing inside a library-style module makes it awkward to embed; gate behind `sys.stdout.isatty()`.
- **I-5** `aurora/agent/loop.py:10` imports `ZoneInfoNotFoundError` but never uses it.
- **I-6** `datetime_tool.py:37` calls `__import__("datetime").timezone.utc` — you already `from datetime import timezone`; just use it.

---

## 4. Code Quality & Performance

### Positives

- Clean package layout (`api`, `agent`, `providers`, `tools`, `memory`) with clear responsibilities.
- Strong typing (`NormalizedMessage`, `StreamEvent`, dataclasses) and async-first design.
- `defusedxml` for RSS parsing — correct choice, avoids XXE.
- `hmac.compare_digest` on API-key compare — correct (timing-safe).
- `aiosqlite` with parameterised queries throughout — SQLi-free.
- Rate limiting present.
- `_http_guards.py` is carefully written — the TOCTOU caveat is even documented in the docstring.
- Nice UX touches: SSE streaming, tool-input deltas, "Generating next action" idle indicator, per-conversation session sandbox.
- Good developer ergonomics in prompts and tool descriptions.

### Negatives / improvement opportunities

- **Copy-paste between tools:** `SSHTool`, `SCPUploadTool`, `ServerProbeTool` all build connection kwargs via `build_connect_kwargs` (good), but the error/host-not-found boilerplate, `_progress_cb` wiring, and `asyncssh` late-import are duplicated. Centralise.
- **Large functions:** `chat_stream()` in `routes/chat.py` is ~200 lines with nested generators — splittable into "persist user message", "prepare history", "pipe agent events", "finalize". Makes maintenance and review easier (and makes the secure approval path more auditable).
- **`_to_normalized` iteration logic** in `chat.py:517-611` is complex and untested. Worth a small unit-test suite; tool history reconstruction is exactly where subtle bugs turn into prompt-injection or info-disclosure.
- **Memory/DB:** each request opens a new SQLite connection; WAL mode is set per connection — the open/close churn is waste. Pool it.
- **Web UI bundle:** `app.js` is a single 84 KB file with ad-hoc SSE state machine. It works, but given growth a small bundler (`esbuild --bundle`) + splitting by concern (`sse.js`, `tools.js`, `media.js`) would pay for itself.

### Performance notes

- `search_solutions` does FTS first, then falls back to `LIKE` with up to 5 OR clauses — OK.
- `RSSFeedTool` fetches all feeds in a category with `asyncio.gather` — good.
- `list_all_sessions()` does `rglob("*")` per session, which is fine for tens of sessions but O(n·files) and synchronous inside an async endpoint. Consider offloading with `asyncio.to_thread`.
- `_current_session` context variable correctly scopes across concurrent requests (each request gets a fresh `Context`).

---

## 5. Prioritised Recommendations

| # | Priority | Action |
|---|---|---|
| 1 | 🔴 | Replace SSH command blacklist with an allow-list and direct-exec (no shell string). Keep the blacklist as telemetry only. |
| 2 | 🟠 | Ship safer defaults in `config.yaml` / `.gitignore`; refuse dangerous combinations in `validate_auth_config`. Rotate the committed SSH key. |
| 3 | 🟠 | Fix DNS-rebinding TOCTOU in `safe_httpx_client` (resolve-once + connect-by-IP). |
| 4 | 🟠 | Route weather tool through `safe_httpx_client`; audit for any other bare `httpx.AsyncClient`. |
| 5 | 🟠 | Don't return the long-lived API key from `/api/login`; use short-lived session tokens, rotate/expire the OTP. |
| 6 | 🟡 | Per-principal conversation ownership & file-session isolation for `/v1` route. |
| 7 | 🟡 | Bind approvals to authenticated session (M-4). |
| 8 | 🟡 | Opt-in proxy trust + prune rate-limit dict (M-2). |
| 9 | 🟡 | File-size / sandbox-quota caps (M-6). |
| 10 | 🟡 | SCP destination allow-list (M-3). |
| 11 | 🟢 | Restrict UUID to v4, sanitize error messages, pool DB connections. |
| 12 | ℹ️ | Add unit tests around `sandbox.resolve`, SSH regexes (negative cases!), `_to_normalized`, and `_http_guards.validate_url`. |

---

## 6. Closing Thoughts

Aurora is a thoughtful piece of software — the SSRF guard, CSP, fail-closed auth, session sandbox, and Secure Mode approval flow put it well above the baseline for agentic frameworks. The primary remaining gap is that it still trusts a regex to gate shell commands on a box where the model can be prompt-injected from web content. Ship the allow-list fix (C-1) and the safer auth/config defaults (H-1, H-4), and the overall security score moves from 6.5 to 8+ comfortably.

Nice work overall; the codebase is pleasant to read.
