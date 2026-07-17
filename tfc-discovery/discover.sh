#!/bin/bash

# Cross-platform wrapper for discover.py (TFC/E pre-migration discovery).
# Supports Git Bash, WSL, Cygwin, MSYS2 on Windows and native bash on Unix-like systems.
#
# discover.py reuses the migrator's shared code (scalr_tfc_migrate) instead of
# duplicating an HTTP client, so it has no third-party dependencies beyond what
# the main project already needs - there's no virtual environment to create or
# activate here, just find a Python 3.12+ interpreter and hand off to it. All
# arguments are passed straight through, and env vars (TFC_HOSTNAME, TFC_TOKEN,
# TFC_ORGANIZATION, TFC_PROJECT) are inherited automatically since we exec into
# the same environment.

set -e

# Find Python executable (same detection order as migrate.sh; discover.py's
# own version check is what actually enforces the 3.12+ requirement)
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

find_python() {
    local python_cmd=""

    for cmd in python3.12 python3 python; do
        if command_exists "$cmd"; then
            local version
            version=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
            local major_version
            major_version=$(echo "$version" | cut -d. -f1)
            if [ "$major_version" = "3" ]; then
                python_cmd="$cmd"
                break
            fi
        fi
    done

    if [ -z "$python_cmd" ]; then
        echo "Python 3.x is required but not found. Please install Python 3.x first."
        exit 1
    fi

    echo "$python_cmd"
}

PYTHON_CMD=$(find_python)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PYTHON_CMD" "$SCRIPT_DIR/discover.py" "$@"
