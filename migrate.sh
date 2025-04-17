#!/bin/bash

set -e

# Ensure Python 3.12 is available
if ! command -v python3.12 &> /dev/null; then
    echo "Python 3.12 is required but not found. Please install Python 3.12 first."
    exit 1
fi

# Function to read credentials from file
read_tfrc_credentials() {
    local credentials_file="$HOME/.terraform.d/credentials.tfrc.json"
    if [ -f "$credentials_file" ]; then
        # Read Scalr token
        local scalr_token=$(jq -r ".credentials.\"$SCALR_HOSTNAME\".token" "$credentials_file" 2>/dev/null)
        if [ "$scalr_token" != "null" ]; then
            export SCALR_TOKEN="$scalr_token"
        fi

        if [ -z "$TFC_HOSTNAME" ]; then
          export TFC_HOSTNAME="app.terraform.io"
        fi

        # Read TFC token
        local tfc_token=$(jq -r ".credentials.\"$TFC_HOSTNAME\".token" "$credentials_file" 2>/dev/null)
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
        --scalr-hostname)
            export SCALR_HOSTNAME="$2"
            shift 2
            ;;
        --scalr-token)
            export SCALR_TOKEN="$2"
            shift 2
            ;;
        --scalr-environment)
            export SCALR_ENVIRONMENT="$2"
            shift 2
            ;;
        --tfc-hostname)
            export TFC_HOSTNAME="$2"
            shift 2
            ;;
        --tfc-token)
            export TFC_TOKEN="$2"
            shift 2
            ;;
        --tfc-organization)
            export TFC_ORGANIZATION="$2"
            shift 2
            ;;
        --tfc-project)
            export TFC_PROJECT="$2"
            shift 2
            ;;
        -v|--vcs-name)
            export SCALR_VCS_NAME="$2"
            shift 2
            ;;
        --pc-name)
            export SCALR_PC_NAME="$2"
            shift 2
            ;;
        -w|--workspaces)
            export WORKSPACES="$2"
            shift 2
            ;;
        --skip-workspace-creation)
            export SKIP_WORKSPACE_CREATION=true
            shift
            ;;
        --skip-backend-secrets)
            export SKIP_BACKEND_SECRETS=true
            shift
            ;;
        --skip-tfc-lock)
            export SKIP_TFC_LOCK=true
            shift
            ;;
        --management-env-name)
            export MANAGEMENT_ENV_NAME="$2"
            shift 2
            ;;
        --disable-deletion-protection)
            export DISABLE_DELETION_PROTECTION=true
            shift
            ;;
        --skip-variables)
            export SKIP_VARIABLES="$2"
            shift 2
            ;;
        --agent-pool-name)
            export SCALR_AGENT_POOL_NAME="$2"
            shift 2
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

# Read credentials from file if not provided
read_tfrc_credentials

# Validate required parameters
validate_required_params

# Set default values if not provided
MANAGEMENT_ENV_NAME=${MANAGEMENT_ENV_NAME:-$DEFAULT_MANAGEMENT_ENV_NAME}

if [ -z "$SCALR_ENVIRONMENT" ]; then
    export SCALR_ENVIRONMENT=${TFC_PROJECT:-$TFC_ORGANIZATION}
fi

install_dependencies=false
# Create and activate virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3.12 -m venv venv
    install_dependencies=true
fi

echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies only on first execution
if [ "$install_dependencies" = true ]; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
fi

# Build the command
CMD="python3.12 migrator.py"
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
    
    # Run post-migration script if it exists
    echo "Starting post-migration steps..."

    # Example: Navigate to the generated Terraform directory
    cd "./generated-terraform/$SCALR_ENVIRONMENT" || exit 1

    pwd
    terraform fmt
    terraform init
    terraform apply

    echo "Post-migration steps completed successfully!"
else
    echo "Migration failed. Please check the errors above."
    exit 1
fi 