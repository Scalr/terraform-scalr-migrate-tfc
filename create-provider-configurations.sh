#!/bin/bash

# Wrapper around create_provider_configurations.py
# Prepares the Python environment the same way migrate.sh does (Python 3.x detection,
# virtualenv, dependencies, credentials from ~/.terraform.d/credentials.tfrc.json) and
# forwards every argument to the Python script, which is where the flags are documented:
#
#   ./create-provider-configurations.sh --help

set -e

# Detect operating system
detect_os() {
    case "$(uname -s)" in
        CYGWIN*|MINGW32*|MINGW64*|MSYS*) OS="windows" ;;
        *) OS="unix" ;;
    esac
}

# Check if command exists cross-platform
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Find Python executable
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

# Get user home directory cross-platform
get_home_dir() {
    if [ "$OS" = "windows" ]; then
        echo "${USERPROFILE:-$HOME}"
    else
        echo "$HOME"
    fi
}

# Activate virtual environment cross-platform
activate_venv() {
    if [ "$OS" = "windows" ]; then
        source venv/Scripts/activate
    else
        source venv/bin/activate
    fi
}

# Hostnames may come from the environment or from the forwarded arguments; the tokens are
# looked up per hostname, so both forms have to be inspected without consuming the arguments.
read_hostnames_from_args() {
    local previous=""
    for arg in "$@"; do
        case "$arg" in
            --scalr-hostname=*) SCALR_HOSTNAME="${arg#*=}" ;;
            --tfc-hostname=*) TFC_HOSTNAME="${arg#*=}" ;;
            *)
                case "$previous" in
                    --scalr-hostname) SCALR_HOSTNAME="$arg" ;;
                    --tfc-hostname) TFC_HOSTNAME="$arg" ;;
                esac
                ;;
        esac
        previous="$arg"
    done
}

# Function to read credentials from file
read_tfrc_credentials() {
    local credentials_file="$USER_HOME/.terraform.d/credentials.tfrc.json"
    if [ -f "$credentials_file" ]; then
        if ! command_exists "jq"; then
            echo "Warning: jq is not available. Cannot read credentials from $credentials_file"
            echo "Please install jq or provide tokens manually via command line arguments."
            return
        fi

        if [ -z "$SCALR_TOKEN" ] && [ -n "$SCALR_HOSTNAME" ]; then
            local scalr_token
            scalr_token=$(jq -r ".credentials.\"$SCALR_HOSTNAME\".token" "$credentials_file" 2>/dev/null)
            if [ "$scalr_token" != "null" ] && [ -n "$scalr_token" ]; then
                export SCALR_TOKEN="$scalr_token"
            fi
        fi

        if [ -z "$TFC_HOSTNAME" ]; then
            export TFC_HOSTNAME="app.terraform.io"
        fi

        if [ -z "$TFC_TOKEN" ]; then
            local tfc_token
            tfc_token=$(jq -r ".credentials.\"$TFC_HOSTNAME\".token" "$credentials_file" 2>/dev/null)
            if [ "$tfc_token" != "null" ] && [ -n "$tfc_token" ]; then
                export TFC_TOKEN="$tfc_token"
            fi
        fi
    fi
}

detect_os
PYTHON_CMD=$(find_python)
USER_HOME=$(get_home_dir)

read_hostnames_from_args "$@"
[ -n "$SCALR_HOSTNAME" ] && export SCALR_HOSTNAME
[ -n "$TFC_HOSTNAME" ] && export TFC_HOSTNAME
read_tfrc_credentials

install_dependencies=false
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    "$PYTHON_CMD" -m venv venv
    install_dependencies=true
fi

echo "Activating virtual environment..."
activate_venv

if [ "$install_dependencies" = true ]; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
fi

# The script's exit code has to survive `set -e` so it can be reported to the caller.
set +e
"$PYTHON_CMD" create_provider_configurations.py "$@"
status=$?
set -e

deactivate

exit $status
