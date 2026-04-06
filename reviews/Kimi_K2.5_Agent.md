# Aurora Security Audit Report

**Repository:** https://github.com/c0m4r/aurora  
**Auditor:** Kimi K2.5 Agent  
**Date:** 2026-04-06  
**Version Audited:** 0.2.0

---

## Executive Summary

Aurora is a general-purpose AI assistant with Linux server administration capabilities, featuring a FastAPI backend, web UI, and CLI client. This audit identified **1 Critical**, **3 High**, **4 Medium**, and **5 Low** severity issues. The most critical finding is that OpenAI-compatible endpoints lack authentication, allowing unauthenticated access to the entire system.

### Risk Overview

| Severity | Count | Status |
|----------|-------|--------|
| Critical | 1 | Requires immediate attention |
| High | 3 | Should be fixed before production |
| Medium | 4 | Should be addressed soon |
| Low | 5 | Recommended improvements |

---

## Critical Severity Issues

### AUR-001: Missing Authentication on OpenAI-Compatible Endpoints

**Severity:** Critical  
**CVSS Score:** 9.1 (CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N)  
**Location:** `aurora/api/routes/compat.py`

#### Description

The OpenAI-compatible `/v1` endpoints (`/v1/models` and `/v1/chat/completions`) do not apply the `require_api_key` authentication dependency. This allows any unauthenticated attacker to:

1. List all available models
2. Execute arbitrary chat completions with tool access (SSH, file operations, web search)
3. Access and modify data through the agent loop

#### Vulnerable Code

```python
# aurora/api/routes/compat.py

@router.get("/models")
async def oai_list_models():  # No auth dependency!
    ...

@router.post("/chat/completions")
async def oai_chat_completions(req: OAIRequest):  # No auth dependency!
    ...
```

#### Proof of Concept

```bash
# List models without authentication
curl http://localhost:8000/v1/models

# Execute chat completion with tool access
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "anthropic/claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "List all files in the files directory"}],
    "stream": false
  }'
```

#### Impact

- Complete bypass of API key authentication
- Unauthorized access to all AI assistant capabilities
- Potential data exfiltration via file_read tool
- Potential command execution on configured SSH hosts

#### Recommendation

Add the `require_api_key` dependency to all `/v1` endpoints:

```python
from ...api.auth import require_api_key

@router.get("/models")
async def oai_list_models(_auth: str = Depends(require_api_key)):
    ...

@router.post("/chat/completions")
async def oai_chat_completions(req: OAIRequest, _auth: str = Depends(require_api_key)):
    ...
```

---

## High Severity Issues

### AUR-002: Overly Permissive CORS Configuration

**Severity:** High  
**CVSS Score:** 7.5 (CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N)  
**Location:** `aurora/api/app.py:50-56`

#### Description

The CORS middleware is configured to allow all origins (`*`) while also allowing credentials. This is a security anti-pattern that can lead to:

- Cross-origin attacks from malicious websites
- Potential API key theft via authenticated cross-origin requests
- CSRF-like attacks against the API

#### Vulnerable Code

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # Allows any origin
    allow_credentials=True,     # Allows cookies/auth headers
    allow_methods=["*"],
    allow_headers=["*"],
)
```

#### Impact

- Malicious websites can make authenticated requests to the API
- API keys stored in browser localStorage could be exfiltrated
- Potential for cross-origin data theft

#### Recommendation

Restrict CORS to specific origins or make credentials conditional:

```python
# Option 1: Restrict origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "https://yourdomain.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Option 2: Disable credentials for wildcard origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # Must be False with wildcard
    allow_methods=["*"],
    allow_headers=["*"],
)
```

---

### AUR-003: SSH Host Key Verification Disabled

**Severity:** High  
**CVSS Score:** 7.4 (CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:N)  
**Location:** `aurora/tools/ssh_tool.py:217`

#### Description

The SSH tool explicitly disables host key verification by setting `known_hosts: None`. This makes the application vulnerable to man-in-the-middle (MITM) attacks, where an attacker can intercept SSH connections and potentially:

- Capture sensitive command output
- Execute malicious commands on the target server
- Exfiltrate data from remote systems

#### Vulnerable Code

```python
connect_kw: dict[str, Any] = {
    "host":      host_cfg.get("host", host),
    "port":      int(host_cfg.get("port", 22)),
    "username":  host_cfg.get("user", "root"),
    "known_hosts": None,  # Accept any host key - DANGEROUS!
}
```

#### Impact

- Man-in-the-middle attacks on SSH connections
- Potential credential theft
- Unauthorized server access
- Data exfiltration from remote systems

#### Recommendation

Require proper host key verification:

```python
known_hosts_file = host_cfg.get("known_hosts_file", "~/.ssh/known_hosts")
connect_kw: dict[str, Any] = {
    "host":      host_cfg.get("host", host),
    "port":      int(host_cfg.get("port", 22)),
    "username":  host_cfg.get("user", "root"),
    "known_hosts": str(Path(known_hosts_file).expanduser()),
}
```

Add configuration option in `config.example.yaml`:

```yaml
tools:
  ssh:
    hosts:
      - name: "web-01"
        host: "10.0.0.10"
        known_hosts_file: "~/.ssh/known_hosts"  # Required for security
```

---

### AUR-004: Path Traversal Vulnerability in File Operations

**Severity:** High  
**CVSS Score:** 7.5 (CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N)  
**Location:** `aurora/tools/file_tool.py:25-35`

#### Description

The path traversal protection in `_resolve()` can be bypassed using Unicode normalization attacks or case sensitivity issues on certain filesystems. The current implementation only checks if the resolved path is within the sandbox using `relative_to()`, but doesn't account for:

- Unicode normalization differences (NFC vs NFD)
- Case sensitivity on Windows/macOS
- Symbolic link traversal
- Path length limits

#### Vulnerable Code

```python
def _resolve(rel_path: str) -> Path | None:
    """Resolve a relative path inside the sandbox. Returns None on traversal."""
    sandbox = _sandbox()
    clean = rel_path.lstrip("/").lstrip("./")
    resolved = (sandbox / clean).resolve()
    try:
        resolved.relative_to(sandbox.resolve())
        return resolved
    except ValueError:
        return None
```

#### Proof of Concept

```python
# Potential bypass using symlinks
# 1. Create a symlink inside files/ pointing to /
# 2. Access the symlink to read arbitrary files

# Or using case sensitivity on Windows:
# Files/secret.txt vs files/secret.txt
```

#### Impact

- Arbitrary file read outside the sandbox
- Potential exposure of sensitive files (/etc/passwd, SSH keys, etc.)
- Information disclosure

#### Recommendation

Implement more robust path validation:

```python
def _resolve(rel_path: str) -> Path | None:
    """Resolve a relative path inside the sandbox. Returns None on traversal."""
    import unicodedata
    
    sandbox = _sandbox().resolve()
    
    # Normalize Unicode to prevent NFC/NFD bypasses
    rel_path = unicodedata.normalize('NFC', rel_path)
    
    # Reject paths with null bytes
    if '\x00' in rel_path:
        return None
    
    # Clean the path
    clean = rel_path.lstrip("/").lstrip("./")
    
    # Reject paths containing .. components after cleaning
    if '..' in Path(clean).parts:
        return None
    
    resolved = (sandbox / clean).resolve()
    
    # Verify the resolved path is within sandbox
    try:
        resolved.relative_to(sandbox)
        # Additional check: verify no symlinks escape sandbox
        for part in resolved.relative_to(sandbox).parts:
            check_path = sandbox / part
            if check_path.is_symlink():
                link_target = check_path.readlink()
                if link_target.is_absolute() or '..' in link_target.parts:
                    return None
        return resolved
    except ValueError:
        return None
```

---

## Medium Severity Issues

### AUR-005: Timing-Safe API Key Comparison Missing

**Severity:** Medium  
**CVSS Score:** 5.9 (CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N)  
**Location:** `aurora/api/auth.py:25`

#### Description

The API key comparison uses a simple string equality check (`!=`), which is vulnerable to timing attacks. An attacker could measure response times to guess the API key character by character.

#### Vulnerable Code

```python
if provided != expected:
    raise HTTPException(status_code=401, detail="Invalid or missing API key")
```

#### Recommendation

Use constant-time comparison:

```python
import hmac

if not hmac.compare_digest(provided, expected):
    raise HTTPException(status_code=401, detail="Invalid or missing API key")
```

---

### AUR-006: No Rate Limiting

**Severity:** Medium  
**CVSS Score:** 5.3 (CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L)  
**Location:** All API endpoints

#### Description

The application lacks any rate limiting, making it vulnerable to:

- Brute force attacks on API keys
- Denial of service through resource exhaustion
- Excessive LLM API usage (cost attacks)
- Database exhaustion via conversation creation

#### Recommendation

Implement rate limiting using `slowapi` or similar:

```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@router.post("/chat/stream")
@limiter.limit("10/minute")
async def chat_stream(req: ChatRequest, _auth: str = Depends(require_api_key)):
    ...
```

---

### AUR-007: Server-Side Request Forgery (SSRF) in RSS Tool

**Severity:** Medium  
**CVSS Score:** 6.5 (CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N)  
**Location:** `aurora/tools/rss_tool.py:284-292`

#### Description

The RSS tool allows fetching arbitrary URLs via the `url` parameter without proper validation. This could be exploited to:

- Access internal services (localhost, private IPs)
- Scan internal network ports
- Access cloud metadata endpoints (169.254.169.254)
- Bypass firewall restrictions

#### Vulnerable Code

```python
async def execute(
    self,
    feed: str | None = None,
    category: str | None = None,
    url: str | None = None,  # Arbitrary URL accepted
    max_items: int | None = None,
    **_: Any,
) -> str:
    ...
    if url:
        return await self._fetch_and_format(url, url, n)
```

#### Recommendation

Validate URLs to prevent SSRF:

```python
from urllib.parse import urlparse
import ipaddress

def _is_safe_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        
        # Only allow http/https
        if parsed.scheme not in ('http', 'https'):
            return False
        
        # Block localhost and private IPs
        hostname = parsed.hostname
        if not hostname:
            return False
        
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_reserved:
                return False
        except ValueError:
            # Not an IP, check for localhost names
            if hostname in ('localhost', '127.0.0.1', '::1'):
                return False
        
        # Block cloud metadata endpoints
        if hostname in ('169.254.169.254', 'metadata.google.internal'):
            return False
        
        return True
    except Exception:
        return False
```

---

### AUR-008: Inconsistent Environment Variable Name

**Severity:** Medium  
**CVSS Score:** 4.3 (CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:L/I:N/A:N)  
**Location:** `aurora/config.py:38-43`

#### Description

The environment variable for the server API key is named `OPSAGENT_API_KEY` instead of `AURORA_API_KEY`, which is inconsistent with the application name and documentation. This could lead to:

- Configuration confusion
- Users accidentally exposing credentials in the wrong variable
- Security misconfigurations

#### Vulnerable Code

```python
_ENV_MAP: dict[str, list[str]] = {
    "ANTHROPIC_API_KEY":  ["providers", "anthropic", "api_key"],
    "OPENAI_API_KEY":     ["providers", "openai", "api_key"],
    "GEMINI_API_KEY":     ["providers", "gemini", "api_key"],
    "OPSAGENT_API_KEY":   ["server", "api_key"],  # Inconsistent naming
}
```

#### Recommendation

Change to `AURORA_API_KEY` and support both for backward compatibility:

```python
_ENV_MAP: dict[str, list[str]] = {
    "ANTHROPIC_API_KEY":  ["providers", "anthropic", "api_key"],
    "OPENAI_API_KEY":     ["providers", "openai", "api_key"],
    "GEMINI_API_KEY":     ["providers", "gemini", "api_key"],
    "AURORA_API_KEY":     ["server", "api_key"],  # Correct naming
    "OPSAGENT_API_KEY":   ["server", "api_key"],  # Deprecated, for backward compat
}
```

---

## Low Severity Issues

### AUR-009: Weak API Key Validation

**Severity:** Low  
**CVSS Score:** 3.7 (CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:L/A:N)  
**Location:** `aurora/api/auth.py:16-17`

#### Description

The authentication bypass for placeholder API keys uses a simple string comparison that could be bypassed if users set similar but not exact placeholder values.

#### Current Code

```python
if not expected or expected in ("change-me-please", "change-me-in-production"):
    return ""
```

#### Recommendation

Add more comprehensive checks and logging:

```python
import re

WEAK_KEY_PATTERNS = [
    r'^change-me',
    r'^placeholder',
    r'^test',
    r'^default',
    r'^password',
    r'^secret$',
    r'^[a-z]+$',
    r'^\d+$',
]

if not expected:
    logger.warning("No API key configured - authentication disabled")
    return ""

if any(re.match(p, expected, re.I) for p in WEAK_KEY_PATTERNS):
    logger.warning("Weak API key detected - please change it")
    return ""
```

---

### AUR-010: Missing Input Validation on Conversation IDs

**Severity:** Low  
**CVSS Score:** 3.1 (CVSS:3.1/AV:N/AC:H/PR:L/UI:N/S:U/C:N/I:L/A:N)  
**Location:** `aurora/api/routes/chat.py:252-268`

#### Description

Conversation IDs are passed directly to database queries without validation. While aiosqlite uses parameterized queries (preventing SQL injection), invalid IDs could cause:

- Unexpected errors
- Potential information disclosure through error messages
- Application instability

#### Recommendation

Add UUID validation:

```python
import uuid

def _validate_conv_id(cid: str) -> bool:
    try:
        uuid.UUID(cid)
        return True
    except ValueError:
        return False

@router.get("/conversations/{cid}")
async def get_conversation(cid: str, _auth: str = Depends(require_api_key)):
    if not _validate_conv_id(cid):
        raise HTTPException(status_code=400, detail="Invalid conversation ID")
    ...
```

---

### AUR-011: Potential XSS via Solution Content

**Severity:** Low  
**CVSS Score:** 3.1 (CVSS:3.1/AV:N/AC:H/PR:L/UI:R/S:C/C:L/I:N/A:N)  
**Location:** `web/js/app.js:915-926`

#### Description

While the frontend generally uses `escHtml()` for user content, the solution cards rendered in the solutions modal may not properly sanitize all fields, potentially allowing stored XSS if malicious content is saved as a solution.

#### Current Code

```javascript
listEl.innerHTML = sols.map(s => `
  <div class="solution-card">
    <h4>${escHtml(s.title || s.problem.slice(0, 60))}</h4>
    <p><strong>Problem:</strong> ${escHtml(s.problem)}</p>
    <p><strong>Solution:</strong> ${escHtml(s.solution.slice(0, 200))}...</p>
    ...
`).join('');
```

#### Recommendation

Ensure all fields are escaped and consider using DOM methods instead of innerHTML:

```javascript
function createSolutionElement(s) {
    const div = document.createElement('div');
    div.className = 'solution-card';
    
    const title = document.createElement('h4');
    title.textContent = s.title || s.problem.slice(0, 60);
    div.appendChild(title);
    
    const problem = document.createElement('p');
    problem.innerHTML = '<strong>Problem:</strong> ' + escHtml(s.problem);
    div.appendChild(problem);
    
    // ... continue with other fields
    return div;
}
```

---

### AUR-012: Information Disclosure via Error Messages

**Severity:** Low  
**CVSS Score:** 2.7 (CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N)  
**Location:** `aurora/api/routes/chat.py:161-163`

#### Description

Detailed error messages from exceptions are returned to clients, potentially revealing:

- Internal file paths
- Database structure
- Configuration details
- Library versions

#### Current Code

```python
except Exception as exc:
    logger.exception("Agent loop error")
    yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
```

#### Recommendation

Return generic error messages to clients, log details server-side:

```python
except Exception as exc:
    logger.exception("Agent loop error: %s", exc)
    yield f"data: {json.dumps({'type': 'error', 'content': 'An internal error occurred'})}\n\n"
```

---

### AUR-013: Insecure Dependency Installation

**Severity:** Low  
**CVSS Score:** 2.0 (CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:N/I:L/A:N)  
**Location:** `install.sh:31`

#### Description

The install script uses `--uploaded-prior-to` flag with a relative date calculation that may not work correctly on all systems, potentially installing outdated or vulnerable packages.

#### Current Code

```bash
pip install --uploaded-prior-to="$(date +\%Y-\%m-\%d -d "7days ago")" -r requirements.lock
```

#### Recommendation

Use pinned versions in requirements.lock (which is already done) and remove the date filter:

```bash
pip install -r requirements.lock
```

---

## Additional Security Considerations

### Dependency Security

The following dependencies should be monitored for security updates:

| Package | Version | Notes |
|---------|---------|-------|
| fastapi | 0.135.2 | Keep updated |
| cryptography | 46.0.6 | Critical security package |
| asyncssh | 2.22.0 | SSH implementation |
| httpx | 0.28.1 | HTTP client |

### Security Headers

The application lacks security headers. Consider adding:

```python
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.gzip import GZipMiddleware

# Add security headers middleware
@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = "default-src 'self'"
    return response
```

### Logging and Monitoring

Consider implementing:

1. Structured logging for security events
2. Failed authentication attempt tracking
3. Unusual activity detection (e.g., excessive tool usage)
4. Audit logging for SSH commands

---

## Compliance Notes

### AGPL-3.0 License Compliance

The project is licensed under AGPL-3.0. If deployed as a network service:

1. Source code must be made available to users
2. License notices must be preserved
3. Modifications must be documented

### Data Protection

Consider implementing:

1. Data retention policies for conversations
2. User data export/deletion capabilities
3. Encryption at rest for the SQLite database
4. Secure handling of API keys (never log them)

---

## Summary of Recommendations

| Priority | Issue | Effort |
|----------|-------|--------|
| P0 | Fix OpenAI endpoint authentication (AUR-001) | Low |
| P0 | Restrict CORS configuration (AUR-002) | Low |
| P1 | Enable SSH host key verification (AUR-003) | Medium |
| P1 | Strengthen path traversal protection (AUR-004) | Medium |
| P1 | Implement rate limiting (AUR-006) | Medium |
| P2 | Use constant-time API key comparison (AUR-005) | Low |
| P2 | Fix SSRF in RSS tool (AUR-007) | Medium |
| P2 | Standardize environment variable naming (AUR-008) | Low |
| P3 | Implement security headers | Low |
| P3 | Add comprehensive logging | Medium |

---

## Conclusion

Aurora is a well-architected application with good separation of concerns and clean code. However, the critical authentication bypass in the OpenAI-compatible endpoints requires immediate attention before any production deployment. The high-severity issues around CORS, SSH security, and path traversal should also be addressed promptly.

With the recommended fixes applied, Aurora should provide a secure foundation for AI-assisted system administration.

---

*Report generated by Kimi K2.5 Agent on 2026-04-06*
