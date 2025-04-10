#!/bin/bash

set -e

# Ensure Python 3.12 is available
if ! command -v python3.12 &> /dev/null; then
    echo "Python 3.12 is required but not found. Please install Python 3.12 first."
    exit 1
fi

# Default values
DEFAULT_MANAGEMENT_ENV_NAME="terraform-management"
DEFAULT_MANAGEMENT_WORKSPACE_NAME="workspace-management"

if [ -z "$TFC_HOSTNAME" ]; then
    export TFC_HOSTNAME="app.terraform.io"
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
    
    if [ -z "$SCALR_ACCOUNT_ID" ]; then
        missing_params+=("SCALR_ACCOUNT_ID")
    fi
    
    if [ ${#missing_params[@]} -ne 0 ]; then
        echo "Error: Missing required parameters: ${missing_params[*]}"
        echo "Please provide these parameters either through:"
        echo "1. Command-line arguments"
        echo "2. Environment variables"
        echo "3. ~/.terraform.d/credentials.tfrc.json file"
        exit 1
    fi
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --scalr-hostname)
            SCALR_HOSTNAME="$2"
            shift 2
            ;;
        --scalr-token)
            SCALR_TOKEN="$2"
            shift 2
            ;;
        --scalr-environment)
            SCALR_ENVIRONMENT="$2"
            shift 2
            ;;
        --tf-hostname)
            TFC_HOSTNAME="$2"
            shift 2
            ;;
        --tf-token)
            TFC_TOKEN="$2"
            shift 2
            ;;
        --tf-organization)
            TFC_ORGANIZATION="$2"
            shift 2
            ;;
        -a|--account-id)
            SCALR_ACCOUNT_ID="$2"
            shift 2
            ;;
        -v|--vcs-id)
            SCALR_VCS_NAME="$2"
            shift 2
            ;;
        -w|--workspaces)
            WORKSPACES="$2"
            shift 2
            ;;
        --skip-workspace-creation)
            SKIP_WORKSPACE_CREATION=true
            shift
            ;;
        --skip-backend-secrets)
            SKIP_BACKEND_SECRETS=true
            shift
            ;;
        -l|--lock)
            LOCK=true
            shift
            ;;
        --management-env-name)
            MANAGEMENT_ENV_NAME="$2"
            shift 2
            ;;
        --management-workspace-name)
            MANAGEMENT_WORKSPACE_NAME="$2"
            shift 2
            ;;
        --disable-deletion-protection)
            DISABLE_DELETION_PROTECTION=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Read credentials from file if not provided
read_tfrc_credentials

# Validate required parameters
validate_required_params

# Set default values if not provided
MANAGEMENT_ENV_NAME=${MANAGEMENT_ENV_NAME:-$DEFAULT_MANAGEMENT_ENV_NAME}
MANAGEMENT_WORKSPACE_NAME=${MANAGEMENT_WORKSPACE_NAME:-$DEFAULT_MANAGEMENT_WORKSPACE_NAME}

# Create and activate virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3.12 -m venv venv
fi

echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Build the command
CMD="python3 migrator.py"
CMD="$CMD --scalr-hostname \"$SCALR_HOSTNAME\""
CMD="$CMD --scalr-token \"$SCALR_TOKEN\""
[ -n "$SCALR_ENVIRONMENT" ] && CMD="$CMD --scalr-environment \"$SCALR_ENVIRONMENT\""
CMD="$CMD --tf-hostname \"$TFC_HOSTNAME\""
CMD="$CMD --tf-token \"$TFC_TOKEN\""
CMD="$CMD --tf-organization \"$TFC_ORGANIZATION\""
CMD="$CMD -a \"$SCALR_ACCOUNT_ID\""
[ -n "$SCALR_VCS_NAME" ] && CMD="$CMD -v \"$SCALR_VCS_NAME\""
[ -n "$WORKSPACES" ] && CMD="$CMD -w \"$WORKSPACES\""
[ "$SKIP_WORKSPACE_CREATION" = true ] && CMD="$CMD --skip-workspace-creation"
[ "$SKIP_BACKEND_SECRETS" = true ] && CMD="$CMD --skip-backend-secrets"
[ "$LOCK" = true ] && CMD="$CMD -l"
[ -n "$MANAGEMENT_ENV_NAME" ] && CMD="$CMD --management-env-name \"$MANAGEMENT_ENV_NAME\""
[ -n "$MANAGEMENT_WORKSPACE_NAME" ] && CMD="$CMD --management-workspace-name \"$MANAGEMENT_WORKSPACE_NAME\""
[ "$DISABLE_DELETION_PROTECTION" = true ] && CMD="$CMD --disable-deletion-protection"

# Run the migrator
echo "Running migrator..."
eval "$CMD"

# Deactivate virtual environment
deactivate

# Check if migration was successful
if [ $? -eq 0 ]; then
    echo "Migration completed successfully!"
    
    # Run post-migration script if it exists
    if [ -f "post-migration.sh" ]; then
        echo "Running post-migration steps..."
        chmod +x post-migration.sh
        ./post-migration.sh
    else
        echo "No post-migration script found. Skipping post-migration steps."
    fi
else
    echo "Migration failed. Please check the errors above."
    exit 1
fi 