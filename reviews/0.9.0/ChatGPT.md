Aurora is a Python AI assistant with SSH command execution, web search and fetch, local file read/write in `./files`, persistent memory, and an optional OpenAI-compatible API. The repo is organized around `aurora/api`, `aurora/tools`, `aurora/providers`, and `aurora/memory`, and the example config keeps the service on `127.0.0.1` by default while requiring a real API key unless unauthenticated mode is explicitly enabled. ([GitHub][1])

## Findings

1. **[VALID] Medium, 6.8/10: rate-limit source IP can be spoofed via `X-Forwarded-For`**
   The app’s sliding-window limiter keys requests on the first `X-Forwarded-For` value when that header is present, and only falls back to `request.client.host` otherwise. That means any client that can reach the app directly can mint a new apparent IP on every request and sidestep the per-IP limits on `/api/*` and `/v1/*`, unless a reverse proxy strips and rewrites the header before traffic reaches Aurora. For an assistant that can trigger expensive LLM calls, SSH commands, and web fetches, this is an abuse-amplifier rather than just a cosmetic defect.
   Fix: only trust `X-Forwarded-For` behind a known proxy, or better, key limits on a proxy-authenticated header that the edge injects and the client cannot forge. ([GitHub][2])

2. **[VALID] Medium, 6.4/10: `file_read` can enumerate every session’s files**
   The file tool exposes an `all_sessions` flag, and when it is set with an empty or root path it returns `_list_all_sessions()`. That helper walks `./files/sessions/<session_id>/` for every session and returns each session ID prefix plus file names and sizes. The sandbox code makes clear that session data is stored under a shared `./files/sessions/<session_id>/` tree, so this is a cross-session metadata leak, not just a convenience view. In a multi-user deployment, a user who can coax the model into calling `file_read(all_sessions=True)` can learn what other users have named and stored, even if they cannot read the contents.
   Fix: remove `all_sessions` from the model-facing tool surface, or gate it behind a separate admin-only capability and an authorization check tied to the caller’s identity, not just the conversation. ([GitHub][3])

3. **[VALID] Low to medium, 4.9/10: authenticated SSE responses hardcode `Access-Control-Allow-Origin: *`**
   The chat and compatibility streaming responses explicitly add `Access-Control-Allow-Origin: *` in the response headers, including `/api/chat/stream`, `/api/learn`, and the OpenAI-compatible streaming path. The app otherwise uses configurable CORS middleware with an allowlist, so these per-response wildcard headers bypass the configured origin policy for the streamed endpoints. That is not an immediate remote compromise, but it widens browser exposure and makes accidental cross-origin integration easier than the rest of the server suggests.
   Fix: remove the hardcoded wildcard and let the configured CORS middleware set the origin, or emit only the exact allowed origin for the request. ([GitHub][4])

4. **[VALID] Low, 4.2/10: residual SSRF race window in the web fetch guard**
   The HTTP guard is thoughtfully defensive: it blocks private, loopback, link-local, and metadata ranges, disables automatic redirects, and revalidates each redirect hop. But the guard itself documents an important limitation: DNS rebinding races are “only partially” mitigated and “the fetch itself still races.” In other words, the code recognizes that validation and connection are still not perfectly atomic. For direct URL fetches from model output, that leaves a narrow but real residual SSRF risk if an attacker can influence DNS or exploit a timing gap.
   Fix: for the highest-trust deployments, prefer a hard domain allowlist with no direct user-controlled URLs, or pin the resolved IP at the transport layer so validation and connection use the same endpoint. ([GitHub][5])

## What looks solid

The SSH layer is not naive. It defaults to strict host-key verification via `~/.ssh/known_hosts`, supports explicit host-key pinning, and only allows the dangerous “accept any host key” mode when the operator opts in. The write-mode command filter is also more disciplined than a simple blacklist, and the example config keeps SSH and other high-impact tools disabled by default. ([GitHub][6])

## Release recommendation

This codebase is close to publishable, but I would fix the first two findings before immediate public release. The rate-limit trust boundary and the cross-session file listing are the kind of bugs that become user-facing the moment the service is shared or exposed beyond one operator. After that, tighten the SSE CORS headers and decide whether direct URL fetch should remain available in public deployments. ([GitHub][2])

[1]: https://github.com/c0m4r/aurora "GitHub - c0m4r/aurora: A general-purpose AI assistant · GitHub"
[2]: https://raw.githubusercontent.com/c0m4r/aurora/main/aurora/api/app.py "raw.githubusercontent.com"
[3]: https://github.com/c0m4r/aurora/blob/main/aurora/tools/file_tool.py "aurora/aurora/tools/file_tool.py at main · c0m4r/aurora · GitHub"
[4]: https://github.com/c0m4r/aurora/blob/main/aurora/api/routes/chat.py "aurora/aurora/api/routes/chat.py at main · c0m4r/aurora · GitHub"
[5]: https://github.com/c0m4r/aurora/blob/main/aurora/tools/_http_guards.py "aurora/aurora/tools/_http_guards.py at main · c0m4r/aurora · GitHub"
[6]: https://github.com/c0m4r/aurora/raw/refs/heads/main/aurora/tools/_ssh_common.py "raw.githubusercontent.com"
