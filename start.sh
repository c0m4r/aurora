#!/bin/bash

# --locked: run only what uv.lock pins, never re-resolve on start.
exec uv run --locked python run_server.py "$@"
