# Aurora security audit

I reviewed the server, auth, route handlers, tool execution layer, config loading, and file sandboxing. The repo advertises SSH command execution, file read/write access, web search, memory persistence, and OpenAI-compatible API access, so the auth boundary is the whole castle gate here, not a decorative latch. ([GitHub][1])

## Overall risk

**Critical** if this service is reachable by anyone outside a tightly trusted local environment. The main reason is that the native API is authenticated, but the `/v1` compatibility API is not, and that public path feeds directly into the same agent loop and tool registry that can invoke SSH and file tools. ([GitHub][2])

## Findings

### 1) Unauthenticated OpenAI-compatible API exposes the agent and its tools ✅ [fixed]

**Severity: Critical (9.8/10)**

The native chat endpoint requires an API key:

```py
@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, _auth: str = Depends(require_api_key)):
```

But the OpenAI-compatible endpoint does not:

```py
@router.post("/chat/completions")
async def oai_chat_completions(req: OAIRequest):
```

There is no `require_api_key` reference anywhere in `compat.py`, and the handler constructs an `AgentLoop` plus a tool registry before processing the request. In practice, that means an unauthenticated caller can drive model inference and tool use through `/v1/chat/completions`, including any configured SSH or file capabilities. ([GitHub][2])

**Impact:** remote unauthenticated abuse of the assistant, tool-triggered file access, possible SSH command execution, token burn, and data leakage from stored conversations or injected solutions.

**Recommendation:** put the same auth dependency on every `/v1` route, or gate the compatibility router behind an explicit opt-in config flag that defaults to off.

---

### 2) SSH host-key verification is disabled ✅ [fixed]

**Severity: High (8.6/10)**

The SSH tool explicitly sets:

```py
"known_hosts": None,  # accept any
```

That disables host key verification entirely. If an attacker can intercept traffic, spoof DNS, or stand up a rogue SSH endpoint, Aurora will happily talk to the wrong box. For a tool that can run administrative commands, that is a classic man-in-the-middle trapdoor. ([GitHub][3])

**Impact:** credential theft, command interception, false output fed back into the agent, and possible privileged compromise of remote hosts.

**Recommendation:** require known-hosts checking by default, support host-key pinning per host, and make “accept any host key” a loudly dangerous lab-only option.

---

### 3) “Read-only” SSH mode is regex filtering, not a real safety boundary ✅ [fixed]

**Severity: High (8.1/10)**

The SSH tool decides whether a command is safe by pattern matching, then hands the raw command string to `asyncssh`:

```py
if host_writes:
    ok, reason = _is_safe_write(command)
else:
    ok, reason = _is_safe_readonly(command)
...
result = await asyncio.wait_for(
    conn.run(command, check=False, term_type="dumb"),
```

That is a text filter around an arbitrary remote shell command, not a policy engine. It will catch some obvious cases, but it is bypassable by commands that perform writes or dangerous actions without matching the blocked patterns, including scripting runtimes and shell constructs the regex does not understand. The code also comments that the user must “explicitly ask” before write mode, which is a prompt-level rule, not a control boundary. ([GitHub][3])

**Impact:** the “read-only by default” promise can be defeated by prompt injection or creative command construction, leading to unintended state changes on remote systems.

**Recommendation:** replace regex-based filtering with an allowlist of safe diagnostic commands, or move to structured operations such as predefined actions, not arbitrary shell strings.

---

### 4) API key authentication fails open when the configured key is empty or placeholder ✅ [fixed]

**Severity: Medium (6.5/10)**

The auth dependency returns success without checking any credential when the configured key is missing or still set to the example placeholders:

```py
if not expected or expected in ("change-me-please", "change-me-in-production"):
    return ""
```

That means a misconfiguration turns authentication off entirely. The project README also describes this as allowed for local use, which is fine for a laptop, but dangerous as a deployment default because it is easy to forget that the same binary can be bound to `0.0.0.0`. ([GitHub][4])

**Impact:** accidental public exposure of all authenticated routes when the service is deployed with an empty or placeholder key.

**Recommendation:** fail closed by default. If the key is missing, refuse to start or force an explicit `--insecure-local-dev` flag.

---

## Positive note

The file sandbox is one of the better parts of the design. It resolves paths inside `./files` and rejects anything that escapes that root:

```py
resolved = (sandbox / clean).resolve()
resolved.relative_to(sandbox.resolve())
```

I did not find an obvious path traversal in the file read/write tool. That part is doing real work, not cosplay. ([GitHub][5])

## Recommended fix order

1. Protect every `/v1` endpoint with the same auth model as `/api/chat/stream`.
2. Turn on SSH host-key verification and require pinning or known-hosts.
3. Replace regex-based SSH safety checks with a strict allowlist.
4. Make missing or placeholder API keys a startup error, not a silent disable.
5. Tighten CORS before exposing the service beyond local use. The app currently uses wildcard origins and credentials together, which is worth hardening even if the API key issue is fixed. ([GitHub][6])

[1]: https://github.com/c0m4r/aurora "GitHub - c0m4r/aurora: A general-purpose AI assistant with Linux server administration capabilities. · GitHub"
[2]: https://github.com/c0m4r/aurora/blob/main/aurora/api/routes/chat.py "aurora/aurora/api/routes/chat.py at main · c0m4r/aurora · GitHub"
[3]: https://raw.githubusercontent.com/c0m4r/aurora/refs/heads/main/aurora/tools/ssh_tool.py "raw.githubusercontent.com"
[4]: https://raw.githubusercontent.com/c0m4r/aurora/refs/heads/main/aurora/api/auth.py "raw.githubusercontent.com"
[5]: https://github.com/c0m4r/aurora/blob/main/aurora/tools/file_tool.py "aurora/aurora/tools/file_tool.py at main · c0m4r/aurora · GitHub"
[6]: https://raw.githubusercontent.com/c0m4r/aurora/refs/heads/main/aurora/api/app.py "raw.githubusercontent.com"
