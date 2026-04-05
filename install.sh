#!/usr/bin/env bash
# Aurora installer
set -e

cd "$(dirname "$0")"

echo "=== Aurora Setup ==="

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "ERROR: Python 3.11+ required"
  exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')")
if [ "$PY_VER" -lt "311" ]; then
  echo "ERROR: Python 3.11+ required (found $(python3 --version))"
  exit 1
fi

# Create venv if not present
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dependencies…"
pip install --upgrade pip
pip install --uploaded-prior-to="$(date +\%Y-\%m-\%d -d "7days ago")" -r requirements.lock

# Create default config if not present
if [ ! -f "config.yaml" ]; then
  cp config.example.yaml config.yaml
  echo ""
  echo "Created config.yaml — edit it to configure your providers and tools."
fi

echo ""
echo "=== Setup complete! ==="
echo ""
echo "1. Edit config.yaml to add your API keys and tool configuration."
echo ""
echo "2. Start the server:"
echo "   source .venv/bin/activate"
echo "   python run_server.py"
echo "   # or: python run_server.py --port 8080 --config /path/to/config.yaml"
echo ""
echo "3. Open the web UI:"
echo "   http://localhost:8000"
echo ""
echo "4. Use the CLI:"
echo "   aurora                                    # interactive REPL"
echo "   aurora -m 'check disk on all servers'     # single message"
echo "   aurora -s http://remote:8000              # connect to remote server"
echo ""
echo "5. Connect opencode / Cursor:"
echo "   Set base URL to: http://localhost:8000/v1"
echo "   Set API key to: (your api_key from config.yaml)"
