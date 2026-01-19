#!/bin/bash
# claude-jacked installer
# Usage: curl -sSL https://raw.githubusercontent.com/jackneil/claude-jacked/master/install.sh | bash

set -e

echo "Installing claude-jacked..."
echo ""

# Check for pipx
if ! command -v pipx &> /dev/null; then
    echo "pipx not found. Installing pipx first..."
    python3 -m pip install --user pipx
    python3 -m pipx ensurepath
    echo ""
    echo "pipx installed. You may need to restart your shell or run:"
    echo "  source ~/.bashrc  (or ~/.zshrc)"
    echo ""
    echo "Then re-run this installer."
    exit 1
fi

# Install claude-jacked via pipx
echo "Installing claude-jacked via pipx..."
pipx install claude-jacked --force

echo ""

# Run jacked install to set up hooks, skill, agents, commands
echo "Setting up Claude Code integration..."
jacked install

echo ""
echo "Done! Restart Claude Code to activate."
echo ""
echo "Next steps:"
echo "  1. Set up Qdrant credentials (run 'jacked configure' for help)"
echo "  2. Index your sessions: jacked backfill"
echo "  3. Use /jacked in Claude Code to search past sessions"
