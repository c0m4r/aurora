#!/usr/bin/env bash
# Aurora cleanup — removes all untracked and ignored files.
set -e

cd "$(dirname "$0")"

RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${RED}⚠️  WARNING${NC}"
echo -e "${YELLOW}This will run ${CYAN}git clean -xdf${YELLOW} and DELETE every untracked and"
echo -e "ignored file in this repository, including:${NC}"
echo -e "  • ${CYAN}.venv/${NC} (virtual environment)"
echo -e "  • ${CYAN}config.yaml${NC} (your local config + generated api_key)"
echo -e "  • databases, caches, build artifacts, and any other local files"
echo ""
echo -e "${RED}This action is IRREVERSIBLE.${NC}"
echo ""

echo -e "${YELLOW}Files that would be removed:${NC}"
git clean -xdn
echo ""

read -r -p "Type 'yes' to proceed: " confirm
if [ "$confirm" != "yes" ]; then
  echo "Aborted."
  exit 1
fi

git clean -xdf
echo -e "${CYAN}✅ Repository reset to a clean state.${NC}"
