#!/bin/bash

# Cross-platform migration script for Windows and Linux/macOS
# Supports Git Bash, WSL, Cygwin, MSYS2 on Windows and native bash on Unix-like systems
# Automatically detects Python 3.x installation and handles platform-specific paths

set -e

# Detect operating system
detect_os() {
    case "$(uname -s)" in
        CYGWIN*|MINGW32*|MINGW64*|MSYS*) OS="windows" ;;
        *) OS="unix" ;;
    esac
}

# Find Python executable
find_python() {
    local python_cmd=""
    
    # Try different Python commands in order of preference
    for cmd in python3.12 python3 python; do
        if command_exists "$cmd"; then
            # Check if it's Python 3.x
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

# Check if command exists cross-platform
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Initialize OS detection and Python command
detect_os
PYTHON_CMD=$(find_python)
USER_HOME=$(get_home_dir)

# Function to read credentials from file
read_tfrc_credentials() {
    local credentials_file="$USER_HOME/.terraform.d/credentials.tfrc.json"
    if [ -f "$credentials_file" ]; then
        # Check if jq is available
        if ! command_exists "jq"; then
            echo "Warning: jq is not available. Cannot read credentials from $credentials_file"
            echo "Please install jq or provide tokens manually via command line arguments."
            return
        fi
        
        # Read Scalr token
        local scalr_token
        scalr_token=$(jq -r ".credentials.\"$SCALR_HOSTNAME\".token" "$credentials_file" 2>/dev/null)
        if [ "$scalr_token" != "null" ]; then
            export SCALR_TOKEN="$scalr_token"
        fi

        if [ -z "$TFC_HOSTNAME" ]; then
          export TFC_HOSTNAME="app.terraform.io"
        fi

        # Read TFC token
        local tfc_token
        tfc_token=$(jq -r ".credentials.\"$TFC_HOSTNAME\".token" "$credentials_file" 2>/dev/null)
        if [ "$tfc_token" != "null" ]; then
            export TFC_TOKEN="$tfc_token"
        fi
    fi
}

# Function to validate required parameters
validate_required_params() {
    local missing_params=()
    
    if [ -z "$SCALR_HOSTNAME" ]; then
        missing_params+=("SCALR_HOSTNAME")
    fi
    
    if [ -z "$SCALR_TOKEN" ]; then
        missing_params+=("SCALR_TOKEN")
    fi
    
    if [ -z "$TFC_TOKEN" ]; then
        missing_params+=("TFC_TOKEN")
    fi
    
    if [ -z "$TFC_ORGANIZATION" ]; then
        missing_params+=("TFC_ORGANIZATION")
    fi
    
    if [ -z "$SCALR_ENVIRONMENT" ]; then
        missing_params+=("SCALR_ENVIRONMENT")
    fi
    
    if [ -z "$SCALR_VCS_NAME" ] && [ "$SKIP_WORKSPACE_CREATION" != "true" ]; then
        missing_params+=("SCALR_VCS_NAME")
    fi
    
    if [ ${#missing_params[@]} -ne 0 ]; then
        echo "Missing required parameters: ${missing_params[*]}"
        exit 1
    fi
}

# Function to display help
show_help() {
    echo "Usage: $0 [OPTIONS]"
    echo "Migrate workspaces from TFC/E to Scalr"
    echo ""
    echo "Required options:"
    echo "  --scalr-hostname HOSTNAME   Scalr hostname"
    echo "  --scalr-token TOKEN         Scalr token"
    echo "  --tfc-hostname HOSTNAME     TFC/E hostname"
    echo "  --tfc-token TOKEN           TFC/E token"
    echo "  --tfc-organization ORG      TFC/E organization name"
    echo ""
    echo "Optional options:"
    echo "  --tfc-project PROJECT             TFC project name to filter workspaces by"
    echo "  --scalr-environment ENV           Scalr environment to create (default: TFC/E organization name)"
    echo "  --vcs-name NAME                   VCS identifier. Required for creation VCS-driven workspaces."
    echo "  --pc-name NAME                    Provider configuration name to link to workspaces"
    echo "  --workspaces PATTERN              Workspaces to migrate (default: all)"
    echo "  --skip-workspace-creation         Skip creating new workspaces in Scalr"
    echo "  --skip-backend-secrets            Skip creating shell variables in Scalr"
    echo "  --skip-tfc-lock                   Skip locking of the TFC/E workspaces after migration"
    echo "  --skip-post-migration             Skip post-migration Terraform steps (fmt, init, apply)"
    echo "  --management-env-name NAME        Name of the management environment (default: scalr-admin)"
    echo "  --disable-deletion-protection     Disable deletion protection in workspace resources"
    echo "  --skip-variables PATTERNS         Comma-separated list of variable keys to skip, or '*' to skip all variables"
    echo "  --agent-pool-name NAME            Scalr agent pool name"
    echo "  --help                            Show this help message"
    echo ""
    echo "Example:"
    echo "  $0 --scalr-hostname app.scalr.io --scalr-token token --tfc-hostname app.terraform.io --tfc-token token --tfc-organization org --vcs-name vcs"
}

# Parse command line arguments
ARGS=()
while [[ $# -gt 0 ]]; do
    case $1 in
        # Handle equal-sign format first
        -v=*|-w=*|--*=*)
            # Extract parameter name and value
            param="${1#-}"   # Remove first -
            param="${param#-}"  # Remove second - if it exists
            param="${param%=*}"  # Remove =value part
            value="${1#*=}"
            # Convert parameter name to environment variable name
            env_var=$(echo "$param" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
            # Handle special cases
            case $1 in
                -v=*|--vcs-name=*) env_var="SCALR_VCS_NAME" ;;
                -w=*|--workspaces=*) env_var="WORKSPACES" ;;
            esac
            export "$env_var"="$value"
            shift
            ;;
        # Handle space-separated format
        --scalr-hostname|--scalr-token|--scalr-environment|--tfc-hostname|--tfc-token|--tfc-organization|--tfc-project|--vcs-name|--pc-name|--workspaces|--management-env-name|--skip-variables|--agent-pool-name)
            param="${1#--}"  # Remove leading --
            env_var=$(echo "$param" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
            case $1 in
                -v|--vcs-name) env_var="SCALR_VCS_NAME" ;;
                -w|--workspaces) env_var="WORKSPACES" ;;
            esac
            export "$env_var"="$2"
            echo "DEBUG: Setting $env_var=$2"
            shift 2
            ;;
        # Handle short options with space
        -v|-w)
            case $1 in
                -v) env_var="SCALR_VCS_NAME" ;;
                -w) env_var="WORKSPACES" ;;
            esac
            export "$env_var"="$2"
            echo "DEBUG: Setting $env_var=$2"
            shift 2
            ;;
        # Handle boolean flags
        --skip-workspace-creation|--skip-backend-secrets|--skip-tfc-lock|--skip-post-migration|--disable-deletion-protection)
            param="${1#--}"  # Remove leading --
            env_var=$(echo "$param" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
            export "$env_var"=true
            shift
            ;;
        --help)
            show_help
            exit 0
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done

# Set default values if not provided
if [ -z "$SCALR_ENVIRONMENT" ]; then
    if [ -n "$TFC_PROJECT" ]; then
        export SCALR_ENVIRONMENT="$TFC_PROJECT"
    else
        export SCALR_ENVIRONMENT="$TFC_ORGANIZATION"
    fi
fi

# Read credentials from file if not provided
read_tfrc_credentials

# Validate required parameters
validate_required_params

# Set default values if not provided
MANAGEMENT_ENV_NAME=${MANAGEMENT_ENV_NAME:-$DEFAULT_MANAGEMENT_ENV_NAME}

install_dependencies=false
# Create and activate virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    "$PYTHON_CMD" -m venv venv
    install_dependencies=true
fi

echo "Activating virtual environment..."
activate_venv

# Install dependencies only on first execution
if [ "$install_dependencies" = true ]; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
fi

# Build the command
CMD="\"$PYTHON_CMD\" migrator.py"
CMD="$CMD --scalr-hostname \"$SCALR_HOSTNAME\""
CMD="$CMD --scalr-token \"$SCALR_TOKEN\""
CMD="$CMD --scalr-environment \"$SCALR_ENVIRONMENT\""
CMD="$CMD --tfc-hostname \"$TFC_HOSTNAME\""
CMD="$CMD --tfc-token \"$TFC_TOKEN\""
CMD="$CMD --tfc-organization \"$TFC_ORGANIZATION\""
[ -n "$SCALR_VCS_NAME" ] && CMD="$CMD --vcs-name \"$SCALR_VCS_NAME\""
[ -n "$SCALR_PC_NAME" ] && CMD="$CMD --pc-name \"$SCALR_PC_NAME\""
[ -n "$WORKSPACES" ] && CMD="$CMD -w \"$WORKSPACES\""
[ "$SKIP_WORKSPACE_CREATION" = true ] && CMD="$CMD --skip-workspace-creation"
[ "$SKIP_BACKEND_SECRETS" = true ] && CMD="$CMD --skip-backend-secrets"
[ "$SKIP_TFC_LOCK" = true ] && CMD="$CMD --skip-tfc-lock"
[ -n "$MANAGEMENT_ENV_NAME" ] && CMD="$CMD --management-env-name \"$MANAGEMENT_ENV_NAME\""
[ "$DISABLE_DELETION_PROTECTION" = true ] && CMD="$CMD --disable-deletion-protection"
[ -n "$TFC_PROJECT" ] && CMD="$CMD --tfc-project \"$TFC_PROJECT\""
[ -n "$SKIP_VARIABLES" ] && CMD="$CMD --skip-variables \"$SKIP_VARIABLES\""
[ -n "$SCALR_AGENT_POOL_NAME" ] && CMD="$CMD --agent-pool-name \"$SCALR_AGENT_POOL_NAME\""

# Run the migrator
echo "Running migrator..."
eval "$CMD"

# Deactivate virtual environment
deactivate

# Check if migration was successful
if [ $? -eq 0 ]; then
    echo "Migration completed successfully!"
    
    # Run post-migration steps if not skipped
    if [ "$SKIP_POST_MIGRATION" != "true" ]; then
        echo "Starting post-migration steps..."

        # Navigate to the generated Terraform directory
        terraform_dir="./generated-terraform/$SCALR_ENVIRONMENT"
        cd "$terraform_dir" || exit 1

        terraform fmt -list=false
        terraform init
        terraform apply

        echo "Post-migration steps completed successfully!"
    else
        echo "Skipping post-migration steps as requested."
    fi
else
    echo "Migration failed. Please check the errors above."
    exit 1
fi 