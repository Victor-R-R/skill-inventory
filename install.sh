#!/usr/bin/env bash
# install.sh — installs skill-inventory globally

set -euo pipefail

CYAN='\033[36m'; BOLD='\033[1m'; GREEN='\033[32m'; RED='\033[31m'; R='\033[0m'

INSTALL_DIR="$HOME/.local/bin"
SCRIPT_SRC="$(cd "$(dirname "$0")" && pwd)/skill-inventory.py"

echo -e "\n${BOLD}${CYAN}▸ Installing skill-inventory${R}\n"

# Check Python 3
if ! command -v python3 &>/dev/null; then
  echo -e "  ${RED}✗${R}  python3 not found. Install Python 3.9+."
  exit 1
fi

PYTHON_VER=$(python3 -c "import sys; print(sys.version_info.minor)")
if [[ "$PYTHON_VER" -lt 9 ]]; then
  echo -e "  ${RED}✗${R}  Python 3.9+ required (you have 3.${PYTHON_VER})."
  exit 1
fi

# Check curl
if ! command -v curl &>/dev/null; then
  echo -e "  ${RED}✗${R}  curl not found."
  exit 1
fi

# Check ANTHROPIC_API_KEY
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo -e "  ${RED}✗${R}  ANTHROPIC_API_KEY not set."
  echo -e "       Add to your ~/.zshrc:  export ANTHROPIC_API_KEY=\"sk-ant-...\""
  exit 1
fi

# Create ~/.local/bin if it doesn't exist
mkdir -p "$INSTALL_DIR"

# Copy and make executable
cp "$SCRIPT_SRC" "$INSTALL_DIR/skill-inventory"
chmod +x "$INSTALL_DIR/skill-inventory"

echo -e "  ${GREEN}✓${R}  Installed at $INSTALL_DIR/skill-inventory"

# Check if it's in PATH
if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
  echo ""
  echo -e "  Add this line to your ~/.zshrc to use it from any directory:"
  echo -e "  ${CYAN}export PATH=\"\$HOME/.local/bin:\$PATH\"${R}"
  echo -e "  Then: ${CYAN}source ~/.zshrc${R}"
else
  echo -e "  ${GREEN}✓${R}  $INSTALL_DIR is already in your PATH"
  echo ""
  echo -e "  Done. Try: ${CYAN}skill-inventory list${R}"
fi

echo ""
