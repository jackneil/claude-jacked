#!/bin/bash
# claude-jacked installer
# Usage: curl -sSL https://raw.githubusercontent.com/jackneil/claude-jacked/master/install.sh | bash

set -e

echo "Installing claude-jacked..."
echo ""

# Check for uv. This script does not download and run another installer for
# you: install uv with its official installer, then run this script again.
if ! command -v uv &> /dev/null; then
    echo "uv is not installed. jacked installs with uv."
    echo ""
    echo "Install uv with its official installer:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "Other ways to install uv: https://docs.astral.sh/uv/getting-started/installation/"
    echo ""
    echo "Then open a new shell and run this installer again."
    exit 1
fi

# Install claude-jacked via uv
echo "Installing claude-jacked via uv..."
uv tool install claude-jacked --force

echo ""

# Run jacked install to set up hooks, skill, agents, commands
echo "Setting up Claude Code integration..."
jacked install

echo ""
echo "Done! Restart Claude Code to activate."
echo ""
echo "Next steps:"
echo "  1. Open the dashboard: jacked webux"
echo "  2. Add your Claude accounts (Accounts > Add Account)"
echo "  3. Try /dcr, /qa, or /whats-next in Claude Code"
