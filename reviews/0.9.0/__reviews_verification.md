# Security Review Verification: Aurora v0.9.0

This document verifies the security findings from four AI reviews located in `reviews/0.9.0/`.

## 📊 Summary of Findings

| Reviewer | Validated Findings | False Positives / Misses | Risk Assessment |
|----------|--------------------|--------------------------|-----------------|
| **GLM-5.1** | 18 / 20 | 2 (LIKE abuse, SPA shadowing) | **Most Accurate** |
| **Qwen3.6 Plus** | 7 / 7 | 0 (Focused on high-impact) | **Highly Technical** |
| **ChatGPT** | 4 / 4 | 0 | **Concise & Valid** |
| **Grok** | 2 / 5 | 3 (Huge false negatives) | **Too Optimistic** |

---

## 🛑 Critical & High Severity (Validated)

### 1. Permanent API Key Leakage via OTP [GLM AUR-001]
- **Location:** `aurora/api/app.py:191`
- **Verification:** **Valid**. If a user logs in using an OTP generated at startup, the `/api/login` endpoint returns the **permanent** `server.api_key`. This turns the OTP into a permanent key recovery mechanism.
- **Impact:** An attacker who guesses or steals the OTP (which lacks rotation or expiry) obtains indefinite persistent access.

### 2. SSRF DNS Rebinding Race Condition [ChatGPT #4, GLM AUR-004, Qwen CVE-001]
- **Location:** `aurora/tools/_http_guards.py`
- **Verification:** **Valid**. The code performs DNS resolution, checks the IP, and then `httpx` performs a *separate* connection to the hostname.
- **Impact:** Attackers can bypass the SSRF guard by changing DNS records between verification and connection.

### 3. Rate-Limit Spoofing via `X-Forwarded-For` [ChatGPT #1, GLM AUR-019, Qwen CVE-005]
- **Location:** `aurora/api/app.py:76-78`
- **Verification:** **Valid**. The middleware trusts the first value of the `X-Forwarded-For` header without checking if the request came from a trusted proxy.
- **Impact:** Rate limits on login and chat can be bypassed by rotating the header.

### 4. Direct Cross-Session File Listing [ChatGPT #2]
- **Location:** `aurora/tools/file_tool.py:47-50`
- **Verification:** **Valid**. The `file_read` tool implementation allows an `all_sessions=True` flag which returns metadata for files across all users/conversations via `_list_all_sessions()`.
- **Impact:** Metadata/filename leak across session boundaries.

---

## 🟡 Medium Severity (Validated)

### 5. SSE CORS Wildcard Bypasses Policy [ChatGPT #3, GLM AUR-005]
- **Location:** `aurora/api/routes/chat.py:283`, `354`
- **Verification:** **Valid**. `Access-Control-Allow-Origin: *` is hardcoded in `StreamingResponse` headers, overriding any restrictions in `CORSMiddleware`.
- **Impact:** Malicious sites can trigger and read streaming responses if the user has an active session.

### 6. Sandbox Symlink TOCTOU [GLM AUR-012, Qwen CVE-003]
- **Location:** `aurora/tools/sandbox.py:109-114`
- **Verification:** **Valid**. Path resolution resolves symlinks *before* checking the sandbox boundary, creating a window where a file can be swapped for a symlink to an external path (e.g. `/etc/passwd`).

### 7. Weather Tool Bypasses SSRF Guard [GLM AUR-010]
- **Location:** `aurora/tools/weather_tool.py:215`
- **Verification:** **Valid**. Uses raw `httpx.AsyncClient` instead of `safe_httpx_client`.

### 8. `/api/learn` Lacks External Tool Extraction Guard [GLM AUR-006]
- **Location:** `aurora/api/routes/chat.py:293-355`
- **Verification:** **Valid**. Unlike `chat_stream`, this endpoint does not filter out tool results from `ssh`, `web`, or `rss`, allowing prompt injection payloads to be "learned" as solutions.

---

## ❌ False Positives & Dubious Findings

### 1. LIKE Pattern Abuse [GLM AUR-011]
- **Status:** **False Positive (Mostly)**. 
- **Reasoning:** GLM argues that `%` or `_` can be used to enumerate the database. However, `fts_query` is sanitized via `re.sub(r'[^\w\s]', ' ', query)`, which replaces `%` with a space. Only `_` remains (as it's a word character), but its impact in a `LIKE` query is negligible for information disclosure.

### 2. SPA Catch-all Shadowing API Routes [GLM AUR-017]
- **Status:** **False Positive**.
- **Reasoning:** The `serve_spa` function has an explicit check `if path.startswith(("api/", "v1/")): raise HTTPException(404)`. Since FastAPI routes are matched before the catch-all, and the catch-all themselves reject API prefixes, there is no realistic shadowing risk.

### 3. Grok's "No Path-Traversal" and "No SSRF" claims
- **Status:** **False Security Claim**.
- **Reasoning:** Grok completely missed the Symlink TOCTOU and the DNS Rebinding window, providing a false sense of security.

---

## 🛠️ Recommended Next Steps

1. **Fix AUR-001 (Critical):** Change the login response to return a temporary session token instead of the permanent API key.
2. **Fix AUR-004 (High):** Pin resolved IPs in `_http_guards.py` to eliminate the DNS rebinding window.
3. **Fix AUR-019 (High):** Only trust `X-Forwarded-For` from a configurable `trusted_proxies` allowlist.
4. **Fix AUR-005 (High):** Remove hardcoded CORS wildcards from SSE responses.
5. **Fix AUR-006 (Medium):** Apply external tool filtering to the manual `/api/learn` endpoint.
