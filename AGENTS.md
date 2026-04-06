## How to test changes

1. You MUST run commands inside python venv.
2. The venv is installed in .venv dir.
3. Use ./start.sh to start the server or use "source .venv/bin/activate && python run_server.py"
4. Be smart about it and Use timeout command to make sure you're not stuck, i.e. "timeout 2 ./start.sh"

## Security

1. You MUST NOT install new pip packages.
2. You MUST NOT update pip packages.
