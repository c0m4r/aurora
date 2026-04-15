## Project description

A general-purpose AI assistant with Linux server administration capabilities.

### Project structure

Frontend: ./web/
Backend: ./aurora/
CLI: ./cli/

## How to test changes

1. You MUST run commands inside python venv.
2. The venv is installed in .venv dir.
3. Use ./start.sh to start the server
4. Be smart about it and Use timeout command to make sure you're not stuck, i.e. "timeout 2 ./start.sh"

## Security

1. You MUST NOT install new pip packages.
2. You MUST NOT update pip packages.

---

Don't be evil