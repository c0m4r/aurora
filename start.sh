#!/bin/bash

source .venv/bin/activate
.venv/bin/pip install --no-deps .
.venv/bin/python run_server.py "$@"
