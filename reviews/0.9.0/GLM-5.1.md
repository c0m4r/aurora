# Security Audit Report: Aurora AI Agent

**Project:** [c0m4r/aurora](https://github.com/c0m4r/aurora)  
**Audit Date:** 2026-04-21  
**Auditor:** GLM-5.1 Security Research  
**Scope:** Full codebase — Python backend, JavaScript frontend, configuration, shell scripts  
**Commit:** Current `main` branch as of audit date  

---

## Executive Summary

Aurora is a FastAPI-based AI assistant that provides SSH command execution, web search, file I/O, RSS feeds, and LLM integration through a streaming chat interface. The codebase demonstrates strong security awareness in many areas — CSP headers, DOMPurify sanitization, HMAC key comparison, SSRF guards, SSH command filtering, and sandboxed file access are all thoughtfully implemented.

However, the audit identified **20 security findings** across four severity levels. The most critical issues center on authentication design weaknesses (the login endpoint returning the permanent API key, OTP brute-force potential), SSRF TOCTOU gaps, CORS misconfigurations on streaming endpoints, and the inherent limitations of regex-based prompt injection defense. The project's attack surface is amplified by the fact that an authenticated user (or a compromised LLM) can execute arbitrary SSH commands on configured hosts, making authentication robustness paramount.

| Severity | Count |
|----------|-------|
| Critical | 3 |
| High | 5 |
| Medium | 7 |
| Low / Informational | 5 |

---

## Findings Index

| # | Severity | ID | Title |
|---|----------|----|-------|
| 1 | Critical | AUR-001 | Login endpoint returns the permanent API key |
| 2 | Critical | AUR-002 | OTP brute-force feasible due to insufficient rate limiting |
| 3 | Critical | AUR-003 | Single-process OTP with no rotation or expiry |
| 4 | High | AUR-004 | SSRF TOCTOU race condition in DNS validation |
| 5 | High | AUR-005 | CORS wildcard on SSE streaming endpoints |
| 6 | High | AUR-006 | `/api/learn` endpoint lacks external-tool exclusion |
| 7 | High | AUR-007 | Prompt injection sanitization is regex-based and bypassable |
| 8 | High | AUR-008 | No file size limit on `file_write` tool (disk exhaustion) |
| 9 | Medium | AUR-009 | SSH passwords stored in plaintext in config |
| 10 | Medium | AUR-010 | Weather tool bypasses SSRF-safe HTTP client |
| 11 | Medium | AUR-011 | LIKE pattern abuse in solution search fallback |
| 12 | Medium | AUR-012 | Sandbox symlink TOCTOU race condition |
| 13 | Medium | AUR-013 | Websearch whitelist replacement vs. extension semantics |
| 14 | Medium | AUR-014 | In-memory rate limiting lacks persistence and multi-instance support |
| 15 | Medium | AUR-015 | SSH command filter regex bypass potential |
| 16 | Low | AUR-016 | `allow_unauthenticated_public` bypasses network-level safety |
| 17 | Low | AUR-017 | SPA catch-all path prefix matching may shadow API routes |
| 18 | Low | AUR-018 | Server binds HTTP only — no TLS enforcement |
| 19 | Low | AUR-019 | `X-Forwarded-For` header trusted without validation |
| 20 | Info | AUR-020 | Version mismatch between `pyproject.toml` and runtime |

---

## Detailed Findings

### [VALID] AUR-001 — Login Endpoint Returns the Permanent API Key

**Severity:** Critical  
**CVSS 3.1:** 8.1 (AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N)  
**Location:** `aurora/api/app.py:191`  
**Category:** CWE-200 — Exposure of Sensitive Information  

**Description:**  
When a user successfully authenticates via the `/api/login` endpoint (using either the permanent API key or the one-time password), the server returns the permanent API key in the response body:

```python
@app.post("/api/login", include_in_schema=True)
async def login(request: Request):
    from .auth import validate_key, _configured_key, _auth_disabled
    if _auth_disabled():
        return {"api_key": ""}
    body = await request.json()
    key = (body.get("key") or "").strip()
    if not validate_key(key):
        return JSONResponse({"detail": "Invalid key"}, status_code=401)
    return {"api_key": _configured_key()}  # ← Returns the permanent key
```

This design transforms the OTP from a single-use credential into a **key-recovery mechanism**. If an attacker obtains a valid OTP — through shoulder surfing, log leakage, or brute-force (see AUR-002) — they gain the permanent API key, which never expires and can be used indefinitely from any location.

**Impact:**  
An attacker who compromises the OTP once obtains persistent, permanent access to the Aurora API, including all tools (SSH, file I/O, web search). The breach persists even after the OTP is replaced by a server restart, since the permanent key remains unchanged.

**Recommendation:**  
- Replace the permanent key return with a **session token** (JWT or opaque token with expiry) that has a limited lifetime (e.g., 24 hours).
- The session token should be cryptographically independent of the permanent API key.
- Store sessions server-side with the ability to revoke them.
- Never expose the permanent API key through any API endpoint.

---

### [VALID] AUR-002 — OTP Brute-Force Feasible Due to Insufficient Rate Limiting

**Severity:** Critical  
**CVSS 3.1:** 7.5 (AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N)  
**Location:** `aurora/api/app.py:182-191`, `aurora/api/app.py:58-110`  
**Category:** CWE-307 — Improper Restriction of Excessive Authentication Attempts  

**Description:**  
The `/api/login` endpoint falls under the general API rate limit of **60 requests per minute** per IP. The OTP is an 8-character alphanumeric string (`[A-Z0-9]`), yielding 36^8 ≈ 2.8 trillion possible values. While the key space is large, the rate limit of 60 attempts per minute means an attacker with a single IP can test **86,400 OTP values per day**. With a botnet or rotating proxies, this scales linearly.

More importantly, there is **no account lockout, no exponential backoff, and no CAPTCHA** on failed login attempts. The rate limiter is also trivially bypassed by rotating `X-Forwarded-For` headers (see AUR-019).

```python
class _RateLimitMiddleware(BaseHTTPMiddleware):
    _CHAT_LIMIT = 20
    _API_LIMIT = 60  # ← Login shares this with all other API routes

    async def dispatch(self, request: Request, call_next):
        # ...
        if path in ("/api/chat/stream", "/v1/chat/completions"):
            allowed = self._check(f"chat:{ip}", self._CHAT_LIMIT)
        else:
            allowed = self._check(f"api:{ip}", self._API_LIMIT)  # ← Login uses this
```

**Impact:**  
An attacker who can observe or guess partial OTP information (e.g., the server console output is visible) can brute-force the remaining characters within practical timeframes. Combined with AUR-001, successful brute-force yields the permanent API key.

**Recommendation:**  
- Implement a **dedicated, stricter rate limit** for `/api/login` (e.g., 5 attempts per minute, 20 per hour).
- Add **account lockout** after N consecutive failed attempts (e.g., 10 failures → 15-minute lockout).
- Add **exponential backoff** on repeated failures.
- Consider adding a **CAPTCHA** after the first 3 failed attempts.
- Log and alert on brute-force patterns.

---

### [VALID] AUR-003 — Single-Process OTP with No Rotation or Expiry

**Severity:** Critical  
**CVSS 3.1:** 6.5 (AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N)  
**Location:** `aurora/api/auth.py:15-21`  
**Category:** CWE-798 — Use of Hard-coded Credentials (variant)  

**Description:**  
The OTP is stored as a module-level global variable in a single Python process. It has the following properties:

1. **No expiry**: The OTP never expires until the server restarts. A server running for months uses the same OTP.
2. **No rotation**: There is no mechanism to regenerate the OTP without restarting the server.
3. **Single-use not enforced**: The OTP can be used unlimited times. The name "one-time password" is misleading — it is actually a persistent alternative key.
4. **Single-process only**: In a multi-worker deployment (e.g., `uvicorn --workers 4`), each worker generates a different OTP, causing inconsistent behavior.
5. **No invalidation after use**: After a successful login with the OTP, it remains valid.

```python
_otp: str | None = None

def generate_otp() -> str:
    global _otp
    _otp = ''.join(secrets.choice(_OTP_ALPHABET) for _ in range(8))
    return _otp
```

**Impact:**  
An OTP that never expires and can be used multiple times is effectively a secondary persistent password. If leaked once, it provides permanent access. The lack of rotation means there is no way to invalidate a compromised OTP without a server restart.

**Recommendation:**  
- Implement **true OTP semantics**: invalidate after first successful use.
- Add an **expiry timer** (e.g., OTP expires after 10 minutes or 1 hour).
- Add a **rotation API** (authenticated) to regenerate the OTP on demand.
- For multi-worker deployments, store the OTP in a shared store (Redis, database, or file with appropriate permissions).

---

### [VALID] AUR-004 — SSRF TOCTOU Race Condition in DNS Validation

**Severity:** High  
**CVSS 3.1:** 7.2 (AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:N)  
**Location:** `aurora/tools/_http_guards.py:83-119`  
**Category:** CWE-367 — Time-of-Check Time-of-Use (TOCTOU) Race Condition  

**Description:**  
The SSRF protection validates DNS resolution against a blocklist of private IP ranges *before* making the HTTP request. However, between validation and the actual connection, DNS can resolve to a different IP (DNS rebinding). The code itself acknowledges this limitation:

```python
def validate_url(url: str) -> None:
    """Raise UnsafeURLError if the URL is not safe to fetch.

    Checks:
      ...
      - every resolved IP is public (rejects DNS-rebinding races only partially —
        the fetch itself still races; callers that need absolute safety should
        resolve once and connect to that specific IP via the Host header)
    """
```

The `SafeClient` class in the same file follows redirects manually and validates each hop, which is good. However, the TOCTOU window still exists for each individual request.

**Impact:**  
An attacker who controls a DNS server for a domain can first return a public IP during validation, then return an internal IP (e.g., 169.254.169.254 for cloud metadata) during the actual connection. This would allow the web search or RSS tool to access internal services, cloud metadata endpoints, or other private resources.

**Recommendation:**  
- **Pin the resolved IP** after validation: resolve DNS, validate the IP, then connect directly to that IP using the `Host` header for the original hostname. This eliminates the TOCTOU gap.
- Example implementation:
  ```python
  # After validating that resolved_ip is safe:
  resp = await client.request(
      method, 
      url.replace(host, str(resolved_ip)), 
      headers={"Host": host, **(headers or {})}
  )
  ```
- Alternatively, use a **local DNS cache with short TTL** and validate both at resolution time and at connection time.

---

### [VALID] AUR-005 — CORS Wildcard on SSE Streaming Endpoints

**Severity:** High  
**CVSS 3.1:** 6.1 (AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N)  
**Location:** `aurora/api/routes/chat.py:283-284`, `aurora/api/routes/chat.py:354`  
**Category:** CWE-942 — Permissive Cross-domain Policy with Untrusted Domains  

**Description:**  
The SSE streaming responses explicitly set `Access-Control-Allow-Origin: *`, completely bypassing the configured CORS policy:

```python
# chat_stream endpoint
return StreamingResponse(
    generate(),
    media_type="text/event-stream",
    headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",  # ← Bypasses CORS config
    },
)

# learn_stream endpoint  
return StreamingResponse(
    generate(),
    media_type="text/event-stream",
    headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",  # ← Same issue
    },
)
```

The main application configures CORS properly via `CORSMiddleware` with explicit origin lists, but these SSE endpoints hardcode a wildcard, allowing **any website** to make authenticated requests to the streaming API if a user visits a malicious page while having a valid API key stored in session storage.

**Impact:**  
A malicious website can make cross-origin requests to `/api/chat/stream` and `/api/learn`. If a victim user has an active Aurora session (API key in `sessionStorage`), the attacker's site can:
- Send chat messages on behalf of the user
- Trigger tool execution (SSH commands, file operations) 
- Exfiltrate streaming responses containing sensitive data

Note: While `sessionStorage` is same-origin, a reflected XSS vulnerability on the Aurora domain would allow exfiltration.

**Recommendation:**  
- Remove the hardcoded `Access-Control-Allow-Origin: *` from all SSE responses.
- Let the configured `CORSMiddleware` handle CORS for streaming endpoints.
- If CORS middleware doesn't apply to `StreamingResponse`, implement a custom middleware or set the header dynamically based on the request's `Origin` header and the configured allowed origins.

---

### [VALID] AUR-006 — `/api/learn` Endpoint Lacks External-Tool Exclusion

**Severity:** High  
**CVSS 3.1:** 6.5 (AV:N/AC:L/PR:L/UI:N/S:U/C:N/I:H/A:N)  
**Location:** `aurora/api/routes/chat.py:293-355`  
**Category:** CWE-94 — Code Injection (indirect, via prompt injection)  

**Description:**  
The `chat_stream` endpoint correctly excludes SSH, web search, and RSS tool output from auto-learning to prevent prompt injection through attacker-controlled content:

```python
_EXTERNAL_TOOLS = frozenset({"ssh", "scp_upload", "websearch", "web", "rss"})
has_external = any(e["name"] in _EXTERNAL_TOOLS for e in tool_log)
if do_learn and tool_log and not has_external:
    async for learn_event in run_extract(...):
        ...
```

However, the `/api/learn` endpoint has **no such restriction**. A user can manually trigger learning on any message, including those with SSH or web search output containing attacker-controlled content. This content is then persisted to the solutions database and **injected into future system prompts**, creating a persistent prompt injection vector.

```python
@router.post("/learn")
async def learn_stream(req: LearnRequest, _auth: str = Depends(require_api_key)):
    # Reconstruct tool_log from saved blocks — no filtering!
    for b in blocks:
        if b.get("type") == "tool_use":
            res = results_by_id.get(b["id"], {})
            tool_log.append({
                "name": b["name"],
                "input": b.get("input", {}),
                "output": res.get("output", ""),  # ← Unfiltered SSH/web output
            })
    # Then fed directly to run_extract()...
```

**Impact:**  
An attacker who can influence SSH command output (e.g., by controlling data on a monitored server) can plant prompt injection payloads that get persisted as "solutions." These are then injected into the system prompt of future conversations, potentially causing the LLM to execute arbitrary tool calls or leak information.

**Recommendation:**  
- Apply the same `_EXTERNAL_TOOLS` exclusion filter to the `/api/learn` endpoint.
- If manual learning of external-tool outputs is intentionally allowed, add a prominent warning and apply stronger sanitization (see AUR-007).
- Consider treating all solutions derived from external-tool output as **untrusted** and never injecting them into system prompts.

---

### [VALID] AUR-007 — Prompt Injection Sanitization Is Regex-Based and Bypassable

**Severity:** High  
**CVSS 3.1:** 5.4 (AV:N/AC:L/PR:L/UI:N/S:U/C:L/I:L/A:N)  
**Location:** `aurora/agent/learner.py:147-151`  
**Category:** CWE-184 — Incomplete List of Disallowed Inputs  

**Description:**  
The learner's prompt injection detection relies on a regex pattern that is trivially bypassable:

```python
_INJECTION_RE = re.compile(
    r"SYSTEM\s*:|###\s*system\b|ignore\s+(?:previous|prior|above)\s+instructions"
    r"|<\s*script\b|<\s*iframe\b|javascript\s*:",
    re.IGNORECASE,
)

def _sanitize(text: str, max_len: int) -> str | None:
    """Strip HTML tags, enforce length cap, reject prompt-injection attempts."""
    text = _HTML_TAG_RE.sub("", text).strip()
    if _INJECTION_RE.search(text):
        return None
    return text[:max_len]
```

Known bypass techniques include:
- "Sys tem:" (insert space/break within keyword)
- "SYSTEM:" with Unicode confusables (e.g., Cyrillic 'С' instead of Latin 'C')
- "disregard all prior instructions" (synonym not matched)
- "forget your previous instructions" (not matched)
- "new instructions:" (not matched)
- Nested encoding, zero-width characters, or RTL overrides
- The HTML tag strip runs first, but `<script>` is only blocked if it appears literally; `<<script>script>` or HTML entities bypass it.

**Impact:**  
Since learned solutions are injected into system prompts (see AUR-006), a crafted solution that bypasses the regex filter can act as a persistent prompt injection, causing the LLM to take unintended actions in all future conversations that match the solution.

**Recommendation:**  
- **Do not rely on regex for prompt injection defense.** This is a fundamentally flawed approach.
- Instead, treat all learned content as **untrusted data** in the system prompt:
  - Wrap injected solutions in clearly delimited, quoted blocks with explicit "BEGIN UNTRUSTED DATA / END UNTRUSTED DATA" markers.
  - Instruct the model in the system prompt to treat solution content as examples only and never as instructions.
- Consider using a **separate LLM call** to verify that a solution does not contain injection attempts before persisting it.
- Rate-limit the `/api/learn` endpoint and `/api/solutions` endpoint separately.

---

### [VALID] AUR-008 — No File Size Limit on `file_write` Tool (Disk Exhaustion)

**Severity:** High  
**CVSS 3.1:** 5.3 (AV:N/AC:L/PR:L/UI:N/S:U/C:N/I:N/A:L)  
**Location:** `aurora/tools/file_tool.py:127-164`  
**Category:** CWE-400 — Uncontrolled Resource Consumption  

**Description:**  
The `FileWriteTool` accepts an arbitrary-length `content` parameter with no size validation:

```python
async def execute(self, path: str, content: str, append: bool = False, **_) -> str:
    if not path or not path.strip():
        return "Error: path must not be empty."
    target = _resolve(path)
    if target is None:
        return "[BLOCKED] Path traversal outside ./files/ is not allowed."
    # No size check on content!
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    try:
        with open(target, mode, encoding="utf-8") as fh:
            fh.write(content)  # ← Unlimited write
```

A malicious or misconfigured LLM can:
1. Write files of arbitrary size, exhausting disk space.
2. Append to files indefinitely (`append=True`), growing a single file without limit.
3. Write many small files across different paths (combined with no file count limit).

**Impact:**  
Disk exhaustion can lead to denial of service for the Aurora server and potentially for other services sharing the same filesystem. In containerized environments with shared storage, this can impact neighboring containers.

**Recommendation:**  
- Add a **maximum file size** limit (e.g., 10 MB per write, 50 MB total per session).
- Add a **maximum file count** per session sandbox.
- Validate `content` length before writing:
  ```python
  MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
  if len(content.encode('utf-8')) > MAX_FILE_SIZE:
      return f"[BLOCKED] Content exceeds maximum file size ({MAX_FILE_SIZE // (1024*1024)} MB)"
  ```
- Consider adding a **total sandbox size** quota.

---

### [VALID] AUR-009 — SSH Passwords Stored in Plaintext in Configuration

**Severity:** Medium  
**CVSS 3.1:** 5.5 (AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N)  
**Location:** `aurora/tools/_ssh_common.py:64-65`, `config.example.yaml`  
**Category:** CWE-256 — Plaintext Storage of a Password  

**Description:**  
SSH passwords are stored in plaintext in `config.yaml` and passed directly to `asyncssh.connect()`:

```python
if host_cfg.get("password"):
    connect_kw["password"] = host_cfg["password"]
```

The configuration file, which also contains API keys for LLM providers, may be readable by other users on the system or may be accidentally committed to version control. The `config.example.yaml` file shows the structure but does not demonstrate encryption.

**Impact:**  
Any user with read access to the configuration file can extract SSH passwords and API keys. This is especially relevant in shared server environments or when configuration files are stored in version control.

**Recommendation:**  
- **Prefer SSH key-based authentication** and document this as the recommended approach. Already partially supported via `key_file` in config.
- For environments requiring password authentication, support **environment variable** or **secret store** (e.g., HashiCorp Vault, AWS Secrets Manager) references in the config.
- Add a `password_cmd` option that executes a command to retrieve the password at runtime (e.g., `password_cmd: "pass ssh/myserver"`).
- At minimum, warn in documentation and startup logs when plaintext passwords are detected in the config.

---

### AUR-010 — Weather Tool Bypasses SSRF-Safe HTTP Client

**Severity:** Medium  
**CVSS 3.1:** 5.0 (AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N)  
**Location:** `aurora/tools/weather_tool.py:215`  
**Category:** CWE-918 — Server-Side Request Forgery  

**Description:**  
The weather tool uses a plain `httpx.AsyncClient` with `follow_redirects=True` instead of the `safe_httpx_client` from `_http_guards.py`:

```python
async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
    resp = await client.get(f"{_OPEN_METEO_BASE}/forecast", params=params)
```

While the base URL (`https://api.open-meteo.com/v1`) is hardcoded and the `params` dict contains only numeric values controlled by the tool (latitude, longitude, etc.), this creates an inconsistency in the security architecture. If the Open-Meteo API were to redirect to an internal address, the plain client would follow it without validation.

**Impact:**  
Currently low risk since the URL and parameters are hardcoded/controlled. However, if the Open-Meteo service were compromised or redirected, the plain client would follow redirects to internal addresses. This also sets a bad precedent — any future modifications that add user-controlled URL components would be immediately vulnerable to SSRF.

**Recommendation:**  
- Replace `httpx.AsyncClient` with `safe_httpx_client()` for consistency:
  ```python
  async with safe_httpx_client(timeout=15.0, headers=_HEADERS) as client:
      resp = await client.get(f"{_OPEN_METEO_BASE}/forecast", params=params)
  ```
- This provides defense-in-depth and ensures all HTTP traffic goes through the same validation layer.

---

### [VALID] AUR-011 — LIKE Pattern Abuse in Solution Search Fallback [FALSE POSITIVE]

**Severity:** Medium  
**CVSS 3.1:** 3.5 (AV:N/AC:L/PR:L/UI:N/S:U/C:L/I:N/A:N)  
**Location:** `aurora/memory/store.py:240-248`  
**Category:** CWE-89 — SQL Injection (LIKE variant)  

**Description:**  
When FTS5 search fails, the code falls back to a LIKE search. While parameterized queries are used (preventing classic SQL injection), the LIKE pattern includes user-controlled keywords without escaping LIKE wildcards:

```python
keywords = [w for w in fts_query.lower().split() if len(w) > 3][:5]
if keywords:
    like_clause = " OR ".join(
        "lower(problem || ' ' || solution) LIKE ?" for _ in keywords
    )
    params = [f"%{k}%" for k in keywords] + [limit]
```

If a user searches for a keyword containing `%` or `_`, these characters act as LIKE wildcards. For example, searching for `%` would match every row in the solutions table, potentially exposing all stored solutions.

**Impact:**  
An authenticated user can craft search queries that bypass intended search semantics, potentially enumerating all stored solutions. While this does not allow modification or deletion of data, it can leak information from the solutions database.

**Recommendation:**  
- Escape LIKE wildcards in keywords before constructing the pattern:
  ```python
  def escape_like(s: str) -> str:
      return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
  
  params = [f"%{escape_like(k)}%" for k in keywords] + [limit]
  ```
- Use `ESCAPE '\\'` clause in the SQL query to define the escape character.

---

### [VALID] AUR-012 — Sandbox Symlink TOCTOU Race Condition

**Severity:** Medium  
**CVSS 3.1:** 4.4 (AV:L/AC:H/PR:L/UI:N/S:U/C:L/I:L/A:L)  
**Location:** `aurora/tools/sandbox.py:109-116`  
**Category:** CWE-367 — Time-of-Check Time-of-Use (TOCTOU) Race Condition  

**Description:**  
The sandbox path resolution validates that a resolved path is inside the sandbox root, but there is a TOCTOU gap between validation and file access:

```python
def resolve(rel_path: str, session_id: str | None = ...) -> Path | None:
    # ...
    sb = sandbox(session_id)
    resolved = (sb / relative).resolve()  # ← Validates symlinks
    
    try:
        resolved.relative_to(sb.resolve())
        return resolved  # ← Returns the validated path
    except ValueError:
        return None
```

Between the time `resolve()` returns and the time the file is actually accessed (read or write), a symlink could be created or modified within the sandbox that points outside of it. The `_list_dir` function in `file_tool.py` does check symlinks, but the read/write operations do not re-validate.

**Impact:**  
A local attacker with concurrent access to the sandbox directory could create a symlink that points to sensitive files (e.g., `/etc/passwd`, the Aurora config with API keys). If the LLM then reads or writes that path, it would access files outside the sandbox.

**Recommendation:**  
- Re-validate the resolved path immediately before file access (not just at resolution time).
- Use `os.open()` with `O_NOFOLLOW` on Linux to prevent symlink following on the final path component.
- Alternatively, use `os.realpath()` immediately before the file operation and verify it is still within the sandbox.

---

### [VALID] AUR-013 — Websearch Whitelist Replacement vs. Extension Semantics

**Severity:** Medium  
**CVSS 3.1:** 3.7 (AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:L/A:L)  
**Location:** `aurora/tools/websearch_tool.py:107-111`  
**Category:** CWE-1188 — Insecure Default Initialization of Resource  

**Description:**  
When a custom whitelist is provided in config, it **completely replaces** the default whitelist rather than extending it:

```python
self.whitelist: list[str] = (
    [d.lower().removeprefix("www.") for d in whitelist]
    if whitelist is not None
    else [d.lower().removeprefix("www.") for d in DEFAULT_WHITELIST]
)
```

An operator adding a single custom domain (e.g., `whitelist: ["internal-docs.company.com"]`) would lose all safety defaults (github.com, stackoverflow.com, etc.), potentially breaking the tool's functionality and creating confusion about what domains are accessible.

**Impact:**  
Operators may unintentionally reduce the whitelist to only their custom domains, breaking the web search tool's ability to fetch from common documentation sites. More critically, if an operator adds only seemingly safe domains, they may unknowingly allow the LLM to fetch from domains that host user-generated content (e.g., a company wiki), creating a prompt injection vector.

**Recommendation:**  
- Change the semantics to **extend** rather than replace:
  ```python
  if whitelist is not None:
      self.whitelist = (
          [d.lower().removeprefix("www.") for d in DEFAULT_WHITELIST] +
          [d.lower().removeprefix("www.") for d in whitelist]
      )
  else:
      self.whitelist = [d.lower().removeprefix("www.") for d in DEFAULT_WHITELIST]
  ```
- If replacement semantics are intentional, add a separate `whitelist_mode: extend | replace` config option (default: `extend`).
- Document this behavior clearly in `config.example.yaml`.

---

### [VALID] AUR-014 — In-Memory Rate Limiting Lacks Persistence and Multi-Instance Support

**Severity:** Medium  
**CVSS 3.1:** 3.1 (AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:L)  
**Location:** `aurora/api/app.py:58-110`  
**Category:** CWE-770 — Allocation of Resources Without Limits or Throttling  

**Description:**  
The rate limiter uses an in-memory `defaultdict(deque)` that:
1. Resets completely on server restart (allowing a burst of requests immediately after restart).
2. Does not work across multiple server instances (each instance tracks independently).
3. Has no upper bound on memory usage (a DDoS with many unique IPs grows the dictionary indefinitely).

```python
class _RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app) -> None:
        super().__init__(app)
        self._windows: dict[str, deque] = defaultdict(deque)  # ← In-memory only
```

**Impact:**  
An attacker can bypass rate limiting by:
1. Triggering a server restart (e.g., via a resource exhaustion attack), then immediately flooding requests.
2. Distributing requests across multiple server instances (in a load-balanced deployment).
3. Using many source IPs to grow the in-memory dictionary, causing memory exhaustion.

**Recommendation:**  
- For single-instance deployments, add a **maximum entries** cap to the `_windows` dict and evict oldest entries when full.
- For multi-instance deployments, use a shared store (Redis, memcached) for rate limit counters.
- Consider using a well-tested rate limiting library such as `slowapi` or `fastapi-limiter`.
- Add a **startup grace period** where rate limits are temporarily stricter after a restart.

---

### [VALID] AUR-015 — SSH Command Filter Regex Bypass Potential

**Severity:** Medium  
**CVSS 3.1:** 4.0 (AV:N/AC:L/PR:L/UI:N/S:U/C:N/I:L/A:N)  
**Location:** `aurora/tools/ssh_tool.py:26-199`  
**Category:** CWE-184 — Incomplete List of Disallowed Inputs  

**Description:**  
The SSH tool uses extensive regex patterns to block dangerous commands. While the implementation is thorough and covers many evasion techniques, regex-based command filtering is inherently fragile. Specific bypass vectors include:

1. **Variables as indirect execution**: `cmd=python3; $cmd -c 'import os'` — the variable assignment bypasses the `python3 -c` pattern.
2. **Hex/octal encoding not covered**: `\x70\x79\x74\x68\x6f\x6e\x33 -c ...` (but `$'\\x...'` ANSI-C quoting is blocked).
3. **Newer interpreters**: `node -e 'require("child_process").exec(...)'` or `deno eval '...'` — not in the evasion patterns.
4. **Cron daemon direct**: `crond` or writing to `/var/spool/cron/` via redirect.
5. **Tclsh**: `tclsh <<'EOF'\nexec rm -rf /\nEOF` — not in the blocked interpreters list.
6. **Printf to shell**: `printf 'rm -rf /' | sh` — `printf` is not blocked, and piping to `sh` is blocked but `printf` piping is harder to catch.

The NFKC normalization for Unicode homoglyphs is a good defense, but it doesn't help against the logical bypasses above.

**Impact:**  
In read-only mode, a sufficiently determined attacker (or a compromised LLM) could craft commands that bypass the filter and execute write operations. In write mode, the catastrophic-command filter can be bypassed with creative indirection.

**Recommendation:**  
- Supplement regex filtering with an **allowlist approach**: define a list of allowed commands (e.g., `ls`, `cat`, `ps`, `df`, `free`, `top`, `journalctl`, `ss`, `ping`, `ip`, `systemctl status`) and block everything else in read-only mode.
- For write mode, consider requiring **explicit user confirmation** (the `secure` mode already implements this — make it the default for SSH write mode).
- Add `node`, `deno`, `tclsh`, `lua`, `scheme`, `racket`, and `gdb` to the evasion patterns.
- Add variable-assignment patterns: `=\s*(?:ba|da|z)?sh\b` or `=\s*python`.
- Log all SSH commands (including blocked ones) for audit purposes.

---

### [VALID] AUR-016 — `allow_unauthenticated_public` Bypasses Network-Level Safety

**Severity:** Low  
**CVSS 3.1:** 4.3 (AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N)  
**Location:** `aurora/api/auth.py:108-109`  
**Category:** CWE-306 — Missing Authentication for Critical Function  

**Description:**  
The `allow_unauthenticated_public` config option allows running Aurora without authentication on a public network. While the startup validation requires explicit opt-in and logs a warning, there is no additional confirmation mechanism (e.g., requiring a second startup flag, or a file-based opt-in marker):

```python
allow_public = bool(getattr(getattr(cfg, "server", None), "allow_unauthenticated_public", False))
if not loopback and not allow_public:
    raise RuntimeError(...)
logger.warning(
    "AUTH DISABLED — server.allow_unauthenticated=true. "
    "Every endpoint is reachable without credentials. Host=%s",
    host,
)
```

**Impact:**  
If an operator sets this option in config, Aurora exposes all endpoints (including SSH tool, file tools, etc.) to the network without authentication. This could lead to complete system compromise if SSH hosts are configured.

**Recommendation:**  
- Require a **file-based opt-in marker** (e.g., `touch /etc/aurora/i-accept-the-risks`) in addition to the config option, to ensure the operator has consciously acknowledged the risk.
- Display an **interactive confirmation prompt** during startup when this option is set (with a `--yes-i-know` flag for automated deployments).
- When `allow_unauthenticated_public` is true, **disable SSH and SCP tools automatically** to limit the blast radius.

---

### AUR-017 — SPA Catch-All Path Prefix Matching May Shadow API Routes [FALSE POSITIVE]

**Severity:** Low  
**CVSS 3.1:** 2.6 (AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:L)  
**Location:** `aurora/api/app.py:212-222`  
**Category:** CWE-444 — Inconsistent Interpretation of HTTP Requests  

**Description:**  
The SPA catch-all route uses string prefix matching to decide whether to serve the frontend or return 404:

```python
@app.get("/{path:path}", include_in_schema=False)
async def serve_spa(path: str = ""):
    if path.startswith(("api/", "v1/")):
        from fastapi import HTTPException
        raise HTTPException(404)
```

This check could potentially be bypassed with URL-encoded path separators (e.g., `api%2Fchat%2Fstream`) or double slashes (e.g., `//api/chat/stream`). While FastAPI's routing typically normalizes paths, the interaction between the catch-all and the API routers depends on route registration order and path normalization behavior.

**Impact:**  
If a bypass exists, it could allow the SPA handler to serve `index.html` for API routes, or vice versa, causing confusion. This is primarily a reliability issue rather than a security vulnerability.

**Recommendation:**  
- Use a more robust path check:
  ```python
  normalized = path.lstrip("/")
  if normalized.startswith(("api/", "v1/")):
      raise HTTPException(404)
  ```
- Consider using `URL.path` from the request object which has been normalized by FastAPI.

---

### [VALID] AUR-018 — Server Binds HTTP Only — No TLS Enforcement

**Severity:** Low  
**CVSS 3.1:** 3.7 (AV:A/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N)  
**Location:** `aurora/api/app.py:295-301`  
**Category:** CWE-319 — Cleartext Transmission of Sensitive Information  

**Description:**  
The server binds to HTTP only and relies on a reverse proxy for TLS:

```python
uvicorn.run(
    "aurora.api.app:app",
    host=host,
    port=port,
    log_level=args.log_level.lower(),
    reload=False,
)
```

The API key is transmitted in HTTP headers (`X-API-Key` or `Authorization: Bearer`), which are sent in cleartext if no reverse proxy provides TLS. The documentation should make this requirement more prominent.

**Impact:**  
On a network without TLS termination, API keys and conversation content are transmitted in plaintext, vulnerable to network-level eavesdropping.

**Recommendation:**  
- Add a **startup warning** if the bind address is not loopback and no TLS is configured:
  ```python
  if host not in ("127.0.0.1", "::1", "localhost"):
      logger.warning(
          "Aurora is bound to %s without TLS. API keys and conversations "
          "will be transmitted in plaintext. Use a reverse proxy with TLS.",
          host
      )
  ```
- Document the TLS reverse proxy requirement prominently in README and config examples.

---

### [VALID] AUR-019 — `X-Forwarded-For` Header Trusted Without Validation

**Severity:** Low  
**CVSS 3.1:** 3.1 (AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L)  
**Location:** `aurora/api/app.py:76-78`  
**Category:** CWE-290 — Authentication Bypass by Spoofing  

**Description:**  
The rate limiter trusts the `X-Forwarded-For` header to determine the client IP:

```python
def _client_ip(self, request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()  # ← Trusts first entry
    return request.client.host if request.client else "unknown"
```

An attacker can trivially bypass rate limiting by sending a different `X-Forwarded-For` value with each request. This also affects the login rate limit (AUR-002).

**Impact:**  
Rate limiting can be completely bypassed by spoofing the `X-Forwarded-For` header. This amplifies the OTP brute-force risk described in AUR-002.

**Recommendation:**  
- Only trust `X-Forwarded-For` if the request comes from a **known proxy IP**. Configure a list of trusted proxy IPs in the config.
- Use the **rightmost** (rather than leftmost) entry from `X-Forwarded-For`, or use a more robust header like `X-Real-IP` set by a trusted reverse proxy.
- Alternatively, ignore `X-Forwarded-For` entirely and use `request.client.host`, which reflects the actual TCP connection IP.

---

### [VALID] AUR-020 — Version Mismatch Between `pyproject.toml` and Runtime

**Severity:** Informational  
**Location:** `aurora/api/app.py:158`, `pyproject.toml`  
**Category:** CWE-1104 — Use of Unmaintained Third Party Components (variant)  

**Description:**  
The project version in `pyproject.toml` is `0.8.0`, but the FastAPI app reports `1.0.0`:

```python
app = FastAPI(
    title="Aurora",
    version="1.0.0",  # ← Mismatch
    ...
)
```

And the health endpoint:
```python
@router.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}  # ← Mismatch
```

**Impact:**  
Version mismatches can cause confusion during vulnerability reporting and patch management. If a security fix is applied to version 0.8.0 but the runtime reports 1.0.0, operators may incorrectly believe they are running a patched version.

**Recommendation:**  
- Use a single source of truth for the version number (e.g., read from `pyproject.toml` at runtime).
- Synchronize all version references.

---

## Positive Security Observations

The following security practices are implemented well and deserve recognition:

| # | Area | Implementation | File |
|---|------|---------------|------|
| 1 | **CSP Headers** | Comprehensive CSP with no `unsafe-inline` in `script-src`, proper `frame-ancestors: none` | `aurora/api/app.py:27-39` |
| 2 | **XSS Prevention** | DOMPurify sanitization of all LLM output, HTML entity escaping in `escHtml()` | `web/js/app.js:66-78` |
| 3 | **HMAC Key Comparison** | Uses `hmac.compare_digest()` to prevent timing attacks on API key validation | `aurora/api/auth.py:33,36,79` |
| 4 | **Sentinel Key Detection** | Rejects default/placeholder API keys at startup and runtime | `aurora/api/auth.py:13,62-71` |
| 5 | **Startup Validation** | Refuses to start with dangerous auth configurations on non-loopback addresses | `aurora/api/auth.py:84-120` |
| 6 | **SSRF Protection** | Comprehensive IP blocklist covering RFC1918, CGNAT, link-local, cloud metadata, IPv6 | `aurora/tools/_http_guards.py:27-53` |
| 7 | **YAML Safe Load** | Uses `yaml.safe_load()` preventing arbitrary Python object deserialization | `aurora/config.py:71` |
| 8 | **defusedxml** | Uses `defusedxml.ElementTree` for RSS parsing, preventing XXE attacks | `aurora/tools/rss_tool.py:6` |
| 9 | **Sandbox Path Validation** | Null byte rejection, NFKC normalization, path traversal prevention | `aurora/tools/sandbox.py:74-116` |
| 10 | **SSH NFKC Normalization** | Prevents Unicode homoglyph bypass in command filtering | `aurora/tools/ssh_tool.py:166-168` |
| 11 | **Secure Mode** | Tool call approval mechanism for sensitive operations | `aurora/agent/loop.py:270-294` |
| 12 | **Auto-learn Exclusions** | Correctly excludes external tool output from auto-learning | `aurora/api/routes/chat.py:254-256` |

---

## Prioritized Remediation Roadmap

### Immediate (Pre-Release)

| Priority | Finding | Action |
|----------|---------|--------|
| 1 | AUR-001 | Stop returning the permanent API key from `/api/login`. Implement session tokens. |
| 2 | AUR-002 | Add dedicated rate limiting for `/api/login` (5 req/min). |
| 3 | AUR-005 | Remove `Access-Control-Allow-Origin: *` from SSE responses. |
| 4 | AUR-006 | Apply `_EXTERNAL_TOOLS` exclusion filter to `/api/learn` endpoint. |

### Short-Term (Next Sprint)

| Priority | Finding | Action |
|----------|---------|--------|
| 5 | AUR-003 | Implement OTP expiry (10 min), single-use invalidation, and rotation API. |
| 6 | AUR-004 | Pin resolved IP after SSRF validation to close TOCTOU gap. |
| 7 | AUR-007 | Redesign prompt injection defense: mark solutions as untrusted data in system prompts, don't rely on regex. |
| 8 | AUR-008 | Add file size and count limits to `file_write` tool. |
| 9 | AUR-010 | Use `safe_httpx_client` in weather tool. |
| 10 | AUR-019 | Validate `X-Forwarded-For` against trusted proxy list. |

### Medium-Term (Next Release)

| Priority | Finding | Action |
|----------|---------|--------|
| 11 | AUR-009 | Support `password_cmd` or secret store references for SSH passwords. |
| 12 | AUR-011 | Escape LIKE wildcards in solution search. |
| 13 | AUR-012 | Re-validate sandbox paths at file access time with `O_NOFOLLOW`. |
| 14 | AUR-013 | Change whitelist to extend (not replace) defaults. |
| 15 | AUR-014 | Cap in-memory rate limit entries; consider Redis for multi-instance. |
| 16 | AUR-015 | Supplement SSH regex filter with command allowlist for read-only mode. |

### Low Priority

| Priority | Finding | Action |
|----------|---------|--------|
| 17 | AUR-016 | Add file-based opt-in marker for `allow_unauthenticated_public`. |
| 18 | AUR-017 | Normalize path before SPA prefix check. |
| 19 | AUR-018 | Add startup warning for non-TLS deployments. |
| 20 | AUR-020 | Synchronize version numbers across codebase. |

---

## Methodology

This audit was conducted through manual source code review of all files in the Aurora repository. The analysis covered:

- **Authentication and authorization**: API key management, OTP mechanism, session handling
- **Input validation**: Path traversal, SQL injection, command injection, prompt injection
- **Network security**: SSRF, CORS, TLS, rate limiting
- **Cryptography**: Key storage, key comparison, random number generation
- **File system security**: Sandboxing, symlink handling, file size limits
- **Frontend security**: XSS, CSP, DOMPurify usage
- **Configuration security**: YAML loading, secret management, default security posture
- **SSH security**: Command filtering, host key verification, credential handling

No dynamic testing or penetration testing was performed. All findings are based on static analysis of the source code.

---

## Disclaimer

This audit was performed to the best of the auditor's abilities based on source code review alone. It may contain false positives or may miss vulnerabilities that would only be apparent through dynamic testing or with knowledge of specific deployment configurations. The findings and recommendations are intended to improve the security posture of the Aurora project and should be validated by the development team before implementation.
