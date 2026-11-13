#!/usr/bin/env bash
# Aurora installer
set -e

cd "$(dirname "$0")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

echo -e "${MAGENTA}🪼 Aurora Setup${NC}"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo -e "${RED}❌ ERROR: Python 3.11.5+ required${NC}"
  exit 1
fi

if ! python3 -c "import sys; sys.exit(sys.version_info < (3, 11, 5))"; then
  echo -e "${RED}❌ ERROR: Python 3.11.5+ required (found $(python3 --version))${NC}"
  exit 1
fi

echo -e "${GREEN}✅ Python version OK${NC}"

# Check uv
if ! command -v uv &>/dev/null; then
  echo -e "${RED}❌ ERROR: uv required, see https://docs.astral.sh/uv/getting-started/installation/${NC}"
  exit 1
fi

echo -e "${GREEN}✅ $(uv --version | cut -d' ' -f1-2) found${NC}"

echo -e "${BLUE}📦 Installing dependencies…${NC}\n"
# Installs exactly what uv.lock pins (hash-verified) into .venv. --locked
# refuses to re-resolve, so a lockfile out of sync with pyproject.toml or its
# 7-day cooldown fails here instead of pulling in fresh releases.
uv sync --locked
echo -e "\n${GREEN}✅ Dependencies installed${NC}"

# Create default config if not present
if [ ! -f "config.yaml" ]; then
  cp config.example.yaml config.yaml
  API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  sed -i "s|^  api_key: \"\"\$|  api_key: \"$API_KEY\"|" config.yaml
  echo -e "${GREEN}🔑 Generated server api_key in config.yaml${NC}"
fi

echo ""
echo -e "${GREEN}🎉 Setup Complete!${NC}"
echo ""
echo -e "${MAGENTA}💻 Next steps:${NC}"
echo ""
echo -e "  ${YELLOW}1.${NC} Edit ${CYAN}config.yaml${NC}"
echo -e "  ${YELLOW}2.${NC} Start the server with: ${CYAN}./start.sh${NC}"
echo -e "  ${YELLOW}3.${NC} Open the web UI at: ${CYAN}http://localhost:8000${NC}"
echo ""
