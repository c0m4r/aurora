# 🔐 Aurora Security Audit Report
**Repository**: https://github.com/c0m4r/aurora  
**Audit Date**: 2026-04-06  
**Auditor**: Qwen3.6-Plus (Security Researcher, Expert Programmer, Auditor)  
**Version**: 1.0.0 (main branch)

---

## 📋 Executive Summary

Aurora is a general-purpose AI assistant with Linux server administration capabilities, featuring SSH access, web search, file operations, and persistent memory. The architecture uses FastAPI for the backend, async I/O throughout, and supports multiple LLM providers.

### 🎯 Overall Security Rating: **MEDIUM-HIGH RISK**

| Category | Score | Notes |
|----------|-------|-------|
| Authentication | ⚠️ Medium | API key auth present but weak defaults |
| Input Validation | ⚠️ Medium | Regex-based command filtering, potential bypasses |
| Path Traversal | ✅ Good | Sandbox enforcement with pathlib |
| SSH Security | ⚠️ Medium | No host key verification, regex bypass risks |
| Web/SSRF | ⚠️ Medium | Domain whitelist, but content parsing risks |
| Data Protection | ⚠️ Medium | SQLite storage, no encryption at rest |
| API Security | ⚠️ Medium | CORS wildcard, missing rate limiting |

---

## 🔍 Critical Findings

### 🚨 HIGH SEVERITY

#### 1. SSH Command Injection via Regex Bypass (CVE-style)
<!-- remaining -->
**Location**: `aurora/tools/ssh_tool.py`  
**CVSS Score**: 8.8 (High)  
**Affected**: SSH tool in both read-only and write modes

```python
# Vulnerable regex pattern (simplified)
_WRITE_COMMANDS = re.compile(
    r"""
    # Redirects that overwrite / append files
    (?(?!=) # \> but not => or 2>
    \| >\> # append redirect
    # ... many patterns ...
    """,
    re.VERBOSE | re.IGNORECASE,
)
```

**Issue**: The safety filtering relies on regex patterns that can be bypassed through:
- Obfuscation: `echo "rm -rf /" | bash`, `$(echo rm) -rf /`
- Encoding: Base64-encoded commands, URL encoding
- Unicode normalization: Homoglyph attacks in command names
- Shell metacharacters: Backticks, `$()`, process substitution

**Exploitation Example**:
```bash
# Bypass via command substitution
ssh_tool.execute(host="web-01", command="$(printf 'r' 'm') -rf /tmp")

# Bypass via base64
ssh_tool.execute(host="web-01", command="echo 'cm0gLXJmIC90bXA=' | base64 -d | bash")
```

**Recommendation**:
```python
# Use shell parsing + allowlist instead of blocklist
import shlex

def _is_safe_command(command: str, allow_writes: bool) -> tuple[bool, str]:
    try:
        # Parse into tokens to detect obfuscation
        tokens = shlex.split(command)
        
        # Check for dangerous patterns in parsed form
        if any(t.startswith(('-', '$', '`', '(', '<')) for t in tokens):
            return False, "shell metacharacters not allowed"
        
        # Allowlist approach for read-only
        if not allow_writes:
            SAFE_READ_CMDS = {'ls', 'cat', 'grep', 'find', 'df', 'free', 'ps', 'top', 'uptime'}
            if tokens[0] not in SAFE_READ_CMDS:
                return False, f"command '{tokens[0]}' not in read-only allowlist"
        
        return True, ""
    except ValueError:
        return False, "malformed command syntax"
```

#### 2. API Key Authentication Weaknesses ✅ [fixed]
**Location**: `aurora/api/auth.py`  
**CVSS Score**: 7.5 (High)  
**Affected**: All API endpoints

```python
def require_api_key(...):
    # If no key configured or still the example value, allow all
    if not expected or expected in ("change-me-please", "change-me-in-production"):
        return ""  # ⚠️ Unauthenticated access!
```

**Issue**: Default configuration disables authentication, creating a critical exposure if deployed without proper configuration.

**Exploitation**:
```bash
# Default deployment - no auth required
curl http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "run command: rm -rf /"}'
```

**Recommendation**:
```python
# Enforce authentication in production
def require_api_key(...):
    cfg = get()
    expected = getattr(getattr(cfg, "server", None), "api_key", None)
    
    # Fail closed: require valid key always
    if not expected:
        raise HTTPException(401, detail="API key not configured")
    
    if expected in ("change-me-please", "change-me-in-production"):
        logger.critical("Default API key in use - authentication disabled!")
        # Optionally: raise exception instead of logging
        # raise HTTPException(401, detail="Default API key must be changed")
    
    # ... rest of validation
```

#### 3. OpenAI-Compatible Endpoint Missing Auth ✅ [fixed]
**Location**: `aurora/api/routes/compat.py`  
**CVSS Score**: 7.0 (High)

```python
@router.post("/chat/completions")  # No Depends(require_api_key)!
async def oai_chat_completions(req: OAIRequest):
    # Direct access to agent loop without auth check
```

**Issue**: The `/v1/chat/completions` endpoint (used by Cursor, opencode, etc.) bypasses the `require_api_key` dependency, allowing unauthenticated access to the agent.

**Recommendation**:
```python
@router.post("/chat/completions")
async def oai_chat_completions(req: OAIRequest, _auth: str = Depends(require_api_key)):
    # Now requires valid API key
```

---

### ⚠️ MEDIUM SEVERITY

#### 4. Path Traversal via Symlink Attack
<!-- remaining -->
**Location**: `aurora/tools/file_tool.py`  
**CVSS Score**: 6.5 (Medium)

```python
def _resolve(rel_path: str) -> Path | None:
    sandbox = _sandbox()
    clean = rel_path.lstrip("/").lstrip("./")
    resolved = (sandbox / clean).resolve()
    try:
        resolved.relative_to(sandbox.resolve())  # ⚠️ Race condition
        return resolved
    except ValueError:
        return None
```

**Issue**: Time-of-check-time-of-use (TOCTOU) vulnerability. An attacker could:
1. Create a symlink inside `./files/` pointing to `/etc/passwd`
2. Request read of that symlink
3. Between `resolve()` and `read_text()`, the symlink could be exploited

**Recommendation**:
```python
def _resolve(rel_path: str) -> Path | None:
    sandbox = _sandbox().resolve()
    clean = rel_path.lstrip("/").lstrip("./")
    
    # Use os.path.realpath for final resolution
    import os
    target = os.path.realpath(sandbox / clean)
    
    # Ensure resolved path starts with sandbox
    if not target.startswith(str(sandbox) + os.sep) and target != str(sandbox):
        return None
    
    return Path(target)
```

#### 5. SSRF via Web Fetch Whitelist Bypass ✅ [fixed]
**Location**: `aurora/tools/websearch_tool.py`  
**CVSS Score**: 6.0 (Medium)

```python
def _is_whitelisted(self, url: str) -> bool:
    host = urlparse(url).netloc.lower().lstrip("www.")
    return any(host == w or host.endswith("." + w) for w in self.whitelist)
```

**Issue**: Domain matching is susceptible to:
- DNS rebinding attacks
- IDN homograph attacks (e.g., `gіthub.com` with Cyrillic 'і')
- Redirect chains to non-whitelisted domains

**Exploitation**:
```python
# Attacker-controlled domain that redirects
url = "https://attacker.com/redirect?url=http://169.254.169.254/latest/meta-data/"
# If attacker.com is whitelisted or bypasses check via redirect
```

**Recommendation**:
```python
def _is_whitelisted(self, url: str) -> bool:
    from urllib.parse import urlparse
    import idna  # pip install idna
    
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        
        # Normalize internationalized domain names
        host = idna.encode(parsed.netloc.lower().lstrip("www.")).decode('ascii')
        
        # Disable redirects or limit to same-origin
        # Use httpx with follow_redirects=False for initial fetch
        
        return any(host == w or host.endswith("." + w) for w in self.whitelist)
    except Exception:
        return False
```

#### 6. Missing Rate Limiting & DoS Protection
<!-- remaining -->
**Location**: `aurora/api/app.py`, all endpoints  
**CVSS Score**: 5.5 (Medium)

**Issue**: No rate limiting on:
- Chat endpoints (expensive LLM calls)
- SSH tool execution
- File operations

**Impact**: Resource exhaustion, billing attacks (if using paid LLM APIs), SSH brute-force.

**Recommendation**:
```python
# Add slowapi or custom rate limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.post("/api/chat/stream")
@limiter.limit("10/minute")  # Adjust per use case
async def chat_stream(...):
    ...
```

#### 7. Insecure CORS Configuration ✅ [fixed]
**Location**: `aurora/api/app.py`
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ⚠️ Wildcard in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

**Issue**: Wildcard CORS with `allow_credentials=True` violates browser security model and enables credential theft in certain scenarios.

**Recommendation**:
```python
# Restrict to known origins in production
allowed_origins = [
    origin.strip() 
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
)
```

---

### ℹ️ LOW SEVERITY / BEST PRACTICES

#### 8. SQLite Database Without Encryption
<!-- remaining -->
**Location**: `aurora/memory/store.py` (implied)  
**CVSS Score**: 3.7 (Low)

**Issue**: Conversation history, saved solutions, and potentially sensitive data stored in plaintext SQLite.

**Recommendation**:
- Use SQLCipher for at-rest encryption
- Or implement field-level encryption for sensitive fields
- Document data retention policies

#### 9. SSH Host Key Verification Disabled ✅ [fixed]
**Location**: `aurora/tools/ssh_tool.py`
```python
connect_kw: dict[str, Any] = {
    "known_hosts": None,  # ⚠️ Accept any host key!
    ...
}
```

**Issue**: Vulnerable to man-in-the-middle attacks on first connect or if host key changes.

**Recommendation**:
```python
# Load known_hosts file
connect_kw["known_hosts"] = [
    os.path.expanduser("~/.ssh/known_hosts"),
    "/etc/ssh/ssh_known_hosts"
]
# Or implement strict host key checking with user confirmation
```

#### 10. No Input Length Limits on Chat Messages
<!-- remaining -->
**Location**: `aurora/api/routes/chat.py`  
**CVSS Score**: 3.1 (Low)

**Issue**: Unbounded message input could lead to:
- Context window exhaustion
- Prompt injection amplification
- Memory exhaustion

**Recommendation**:
```python
class ChatRequest(BaseModel):
    message: str = Field(..., max_length=32000)  # Adjust per model context
    # ... other fields
```

#### 11. Missing Security Headers in SSE Responses ✅ [fixed]
**Location**: `aurora/api/routes/chat.py`
```python
return StreamingResponse(
    generate(),
    media_type="text/event-stream",
    headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        # Missing: Content-Security-Policy, X-Content-Type-Options, etc.
    },
)
```

**Recommendation**: Add security headers middleware:
```python
@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'"
    return response
```

#### 12. Logging of Sensitive Data
<!-- remaining -->
**Location**: Multiple files  
**CVSS Score**: 3.0 (Low)

**Issue**: Commands, API responses, and potentially credentials may be logged without redaction.

**Recommendation**:
```python
# Implement sensitive data redaction in logs
def _redact_sensitive(value: str) -> str:
    patterns = [
        (r'(api[_-]?key|token|secret)["\']?\s*[:=]\s*["\']?([^"\'\s,}]+)', r'\1: [REDACTED]'),
        (r'password["\']?\s*[:=]\s*["\']?([^"\'\s,}]+)', r'password: [REDACTED]'),
    ]
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value, flags=re.I)
    return value
```

---

## 🛡️ Security Hardening Checklist

### Immediate Actions (Critical)
- [ ] **Fix `/v1/chat/completions` authentication** - Add `Depends(require_api_key)`
- [ ] **Enforce API key in production** - Fail closed if key is default/missing
- [ ] **Harden SSH command filtering** - Replace regex blocklist with parsed allowlist
- [ ] **Enable SSH host key verification** - Load `known_hosts` file

### Short-term Improvements (1-2 weeks)
- [ ] Add rate limiting to all API endpoints
- [ ] Restrict CORS origins to known domains
- [ ] Implement input validation with length limits
- [ ] Add security headers middleware
- [ ] Audit and redact sensitive data in logs

### Medium-term Enhancements (1 month)
- [ ] Encrypt SQLite database with SQLCipher
- [ ] Implement request signing for SSH commands
- [ ] Add audit logging for all tool executions
- [ ] Create security configuration guide with secure defaults
- [ ] Add automated security tests (fuzzing, injection tests)

### Long-term Architecture (Quarterly)
- [ ] Implement RBAC for multi-user deployments
- [ ] Add support for hardware security modules (HSM) for key storage
- [ ] Integrate with SIEM for security monitoring
- [ ] Conduct third-party penetration testing
- [ ] Establish bug bounty program

---

## 📊 Threat Model Summary

| Threat Actor | Capability | Likelihood | Impact | Mitigation |
|-------------|-----------|-----------|--------|-----------|
| External Attacker | Network access to API | High | Critical | Auth enforcement, rate limiting |
| Malicious User | Authenticated API access | Medium | High | Command allowlists, input validation |
| Compromised LLM | Prompt injection | Medium | High | Output filtering, tool result sanitization |
| Insider Threat | Server access | Low | Critical | Audit logging, principle of least privilege |
| Supply Chain | Dependency compromise | Medium | High | Lock dependencies, SBOM, vulnerability scanning |

---

## 🔧 Code Review Recommendations

### SSH Tool Hardening Example
```python
# aurora/tools/ssh_tool.py - Recommended replacement for _is_safe_readonly

import shlex
from typing import Set

SAFE_READ_COMMANDS: Set[str] = {
    'ls', 'cat', 'head', 'tail', 'grep', 'find', 'df', 'du', 'free',
    'uptime', 'top', 'ps', 'pgrep', 'systemctl', 'journalctl', 'ss', 
    'ip', 'ping', 'curl', 'wget', 'docker', 'kubectl', 'nginx', 'httpd'
}

def _is_safe_readonly(command: str) -> tuple[bool, str]:
    """Parse and validate command using allowlist approach."""
    try:
        # Detect and reject obfuscation attempts
        if any(char in command for char in ['`', '$', '<', '>', '|', ';', '&', '\n']):
            # Allow safe pipes for filtering (e.g., grep | head)
            if not _is_safe_pipeline(command):
                return False, "shell metacharacters require explicit approval"
        
        tokens = shlex.split(command)
        if not tokens:
            return False, "empty command"
        
        base_cmd = tokens[0].lstrip('./').split('/')[-1]
        
        # Blocklist: always dangerous
        if base_cmd in {'rm', 'mkfs', 'dd', 'chmod', 'chown', 'useradd', 'reboot'}:
            return False, f"command '{base_cmd}' is blocked"
        
        # Allowlist: safe read operations
        if base_cmd not in SAFE_READ_COMMANDS:
            return False, f"command '{base_cmd}' not in read-only allowlist"
        
        # Validate arguments for specific commands
        if base_cmd == 'systemctl' and any(arg in tokens for arg in ['start', 'stop', 'restart']):
            return False, "systemctl state changes require write mode"
        
        return True, ""
        
    except ValueError as e:
        return False, f"command parsing error: {e}"
```

### Authentication Hardening Example
```python
# aurora/api/auth.py - Production-ready auth

import secrets
import hashlib
from fastapi import Header, HTTPException, status

def require_api_key(
    x_api_key: str | None = Header(None),
    authorization: str | None = Header(None),
) -> str:
    from ..config import get
    cfg = get()
    expected = getattr(getattr(cfg, "server", None), "api_key", None)
    
    # Fail closed: authentication always required
    if not expected:
        logger.critical("API authentication required but not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: API key not set"
        )
    
    # Reject default values explicitly
    if expected in ("change-me-please", "change-me-in-production", ""):
        logger.critical("Default API key detected - refusing requests")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key must be changed from default value"
        )
    
    # Extract provided key
    provided = x_api_key or (
        authorization[7:] if authorization and authorization.startswith("Bearer ") 
        else None
    )
    
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Constant-time comparison to prevent timing attacks
    if not secrets.compare_digest(
        hashlib.sha256(provided.encode()).digest(),
        hashlib.sha256(expected.encode()).digest()
    ):
        logger.warning("Invalid API key attempt from %s", 
                      getattr(request, "client", {}).get("host", "unknown"))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    
    return provided
```

---

## 🧪 Testing Recommendations

### Security Test Cases to Add
```python
# tests/security/test_ssh_tool.py
import pytest

class TestSSHCommandFiltering:
    @pytest.mark.parametrize("command", [
        "rm -rf /",
        "$(echo rm) -rf /tmp", 
        "echo 'cm0gLXJmIC8=' | base64 -d | bash",
        "printf 'r\\x6d' -rf /tmp",  # hex encoding
        "r\u006d -rf /tmp",  # unicode normalization
    ])
    def test_blocks_obfuscated_dangerous_commands(self, command):
        allowed, reason = _is_safe_readonly(command)
        assert not allowed, f"Command should be blocked: {command}"
    
    def test_allows_safe_pipeline(self):
        allowed, reason = _is_safe_readonly("ps aux | grep nginx | head -5")
        assert allowed, f"Safe pipeline blocked: {reason}"

# tests/security/test_auth.py
class TestAuthentication:
    def test_rejects_default_api_key(self):
        # Set config to default value
        with patch_config(server={"api_key": "change-me-please"}):
            with pytest.raises(HTTPException) as exc:
                require_api_key(x_api_key="change-me-please")
            assert exc.value.status_code == 401
    
    def test_constant_time_comparison(self):
        # Test that timing doesn't leak key validity
        import time
        valid_key = "a" * 32
        invalid_keys = ["b" * 32, "a" * 31 + "b", "x" + "a" * 31]
        
        times = []
        for key in [valid_key] + invalid_keys:
            start = time.perf_counter_ns()
            try:
                require_api_key(x_api_key=key)
            except HTTPException:
                pass
            times.append(time.perf_counter_ns() - start)
        
        # All attempts should take similar time (within 20%)
        assert max(times) / min(times) < 1.2, "Timing side-channel detected"
```

---

## 📚 References

1. AsyncSSH Rogue Session Attack (CVE-2023-46446) - Ensure `asyncssh>=2.14.1` [[21]]
2. FastAPI Security Best Practices - API Key Authentication [[14]]
3. Path Traversal Prevention in Python - Use `os.path.realpath()` with prefix checking [[34]]
4. Server-Sent Events Security - Always use HTTPS, validate origins [[41]]
5. OWASP Top 10 for LLM Applications - Prompt injection, insecure output handling

---

## ✅ Conclusion

Aurora demonstrates thoughtful security design with sandboxing, command filtering, and authentication scaffolding. However, **critical gaps in authentication enforcement and command validation** create significant risk if deployed without hardening.

**Priority Order for Fixes**:
1. 🔴 Enforce API key authentication on ALL endpoints (especially `/v1/*`)
2. 🔴 Replace regex-based SSH filtering with parsed allowlist
3. 🟡 Add rate limiting and input validation
4. 🟡 Harden CORS and add security headers
5. 🟢 Implement encryption and audit logging

With these improvements, Aurora can achieve a **LOW RISK** security posture suitable for production deployment in trusted environments.

---

*Report generated by Qwen3.6-Plus | Security Research Division | 2026-04-06*  
*This audit is based on static code analysis of the public repository. Dynamic testing and threat modeling for specific deployment environments are recommended before production use.*
