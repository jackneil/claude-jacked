#!/bin/bash
# claude-jacked uninstaller
# Usage: curl -sSL https://raw.githubusercontent.com/jackneil/claude-jacked/master/uninstall.sh | bash

set -e

echo "Uninstalling claude-jacked..."
echo ""

# Remove Claude Code integration first (while jacked is still installed)
if command -v jacked &> /dev/null; then
    echo "Removing Claude Code integration..."
    jacked uninstall -y
    echo ""
fi

# Uninstall via pipx
if command -v pipx &> /dev/null; then
    echo "Removing claude-jacked package..."
    pipx uninstall claude-jacked 2>/dev/null || echo "Package not installed via pipx"
fi

echo ""
echo "Done! claude-jacked has been removed."
echo ""
echo "Note: Your Qdrant index is still intact if you want to reinstall later."
