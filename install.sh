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
  echo -e "${RED}❌ ERROR: Python 3.11+ required${NC}"
  exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')")
if [ "$PY_VER" -lt "311" ]; then
  echo -e "${RED}❌ ERROR: Python 3.11+ required (found $(python3 --version))${NC}"
  exit 1
fi

echo -e "${GREEN}✅ Python version OK${NC}"

# Create venv if not present
if [ ! -d ".venv" ]; then
  echo -e "${BLUE}📦 Creating virtual environment…${NC}"
  python3 -m venv .venv
else
  echo -e "${GREEN}✅ Virtual environment exists${NC}"
fi

source .venv/bin/activate

echo -e "${BLUE}📦 Installing dependencies…${NC}\n"
pip install --upgrade pip
pip install --uploaded-prior-to="$(date +\%Y-\%m-\%d -d "7days ago")" -r requirements.lock
echo -e "\n${GREEN}✅ Dependencies installed${NC}"

# Create default config if not present
if [ ! -f "config.yaml" ]; then
  cp config.example.yaml config.yaml
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
