# Aurora — Missing Tools & Ideas

## Recommended New Tools

### file_delete
Delete files or directories inside the `./files/` sandbox.
Currently there is no way to clean up files — the sandbox accumulates over time.

### file_download
Download a URL directly to `./files/` as a binary file.
The model currently has to fetch content via `web` then `file_write` the text.
Direct binary download (images, tarballs, PDFs) is not possible.

### scp_download
Download files **from** remote servers to `./files/`.
The inverse of `scp_upload` — useful for pulling logs, configs, backups for analysis.

### shell (local command execution)
Run commands on the **local** machine (sandboxed).
The model can create scripts in `./files/` but can't run them locally.
Useful for data processing, Python scripts, text manipulation, code execution.

### code_exec (Python sandbox)
Execute Python snippets in a sandboxed interpreter.
Data analysis, calculations, regex testing — the model currently has to write
a file and ask the user to run it.

### docker / container
Inspect and manage Docker containers on remote hosts.
Very common sysadmin workflow. A dedicated tool could provide safer abstractions
than raw SSH commands (e.g. `docker ps`, `docker logs`, `docker inspect`).

### cron_viewer
Read and display crontabs from remote servers.
Common sysadmin task; currently requires SSH + manual output parsing.

### clipboard / artifact
Return structured output (code blocks, tables, JSON) as downloadable artifacts.
The web UI could render these as copy-to-clipboard buttons or download links.

## Session-Based File Isolation

Currently all conversations share the same `./files/` directory.
Consider storing files under `./files/<session_id>/` so that:
- Files from one session don't overwrite files from another
- Users can still browse/access files across sessions if needed
- Old sessions can be cleaned up independently
