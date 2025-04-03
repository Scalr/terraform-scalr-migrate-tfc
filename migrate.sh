#!/bin/bash

# Function to read credentials from Terraform credentials file
read_tfrc_credentials() {
    local tfrc_file="$HOME/.terraform.d/credentials.tfrc.json"
    if [ -f "$tfrc_file" ]; then
        # Read Scalr credentials
        if [ -n "$SCALR_HOSTNAME" ]; then
            local scalr_token=$(jq -r ".credentials.\"$SCALR_HOSTNAME\".token" "$tfrc_file" 2>/dev/null)
            if [ -n "$scalr_token" ] && [ "$scalr_token" != "null" ]; then
                export SCALR_TOKEN=${SCALR_TOKEN:-$scalr_token}
            fi
        fi

        # Read TFC/E credentials
        if [ -n "$TF_HOSTNAME" ]; then
            local tf_token=$(jq -r ".credentials.\"$TF_HOSTNAME\".token" "$tfrc_file" 2>/dev/null)
            if [ -n "$tf_token" ] && [ "$tf_token" != "null" ]; then
                TF_TOKEN=${TF_TOKEN:-$tf_token}
            fi
        fi
    fi
}

# Default parameters
SCALR_HOSTNAME=${SCALR_HOSTNAME:-"account.scalr.io"}
SCALR_TOKEN=${SCALR_TOKEN:-""}
SCALR_ENVIRONMENT=${SCALR_ENVIRONMENT:-""}
TF_HOSTNAME=${TF_HOSTNAME:-"app.terraform.io"}
TF_TOKEN=${TF_TOKEN:-""}
TF_ORGANIZATION=${TF_ORGANIZATION:-""}
ACCOUNT_ID=${ACCOUNT_ID:-""}
VCS_ID=${VCS_ID:-""}
WORKSPACES=${WORKSPACES:-"*"}
SKIP_WORKSPACE_CREATION=${SKIP_WORKSPACE_CREATION:-"false"}
SKIP_BACKEND_SECRETS=${SKIP_BACKEND_SECRETS:-"false"}
LOCK=${LOCK:-"false"}
MANAGEMENT_ENV_NAME=${MANAGEMENT_ENV_NAME:-"terraform-management"}
MANAGEMENT_WORKSPACE_NAME=${MANAGEMENT_WORKSPACE_NAME:-"workspace-management"}

# Function to display usage
usage() {
    echo "Usage: $0 [options]"
    echo "Options:"
    echo "  --scalr-hostname HOSTNAME     Scalr hostname (default: $SCALR_HOSTNAME)"
    echo "  --scalr-token TOKEN          Scalr token (will be read from credentials.tfrc.json if not provided)"
    echo "  --scalr-environment ENV      Scalr environment (optional)"
    echo "  --tf-hostname HOSTNAME       TFC/E hostname (default: $TF_HOSTNAME)"
    echo "  --tf-token TOKEN             TFC/E token (will be read from credentials.tfrc.json if not provided)"
    echo "  --tf-organization ORG        TFC/E organization name"
    echo "  --account-id ID              Scalr account ID"
    echo "  --vcs-id ID                  VCS identifier"
    echo "  --workspaces PATTERN         Workspaces to migrate (default: $WORKSPACES)"
    echo "  --skip-workspace-creation    Skip workspace creation in Scalr"
    echo "  --skip-backend-secrets       Skip backend secrets creation"
    echo "  --lock                       Lock TFE workspaces"
    echo "  --management-env-name NAME   Management environment name (default: $MANAGEMENT_ENV_NAME)"
    echo "  --management-workspace-name NAME  Management workspace name (default: $MANAGEMENT_WORKSPACE_NAME)"
    echo "  -h, --help                   Display this help message"
    echo ""
    echo "Note: Tokens can be provided via:"
    echo "  1. Command line arguments"
    echo "  2. Environment variables (SCALR_TOKEN, TF_TOKEN)"
    echo "  3. ~/.terraform.d/credentials.tfrc.json file"
    exit 1
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
            TF_HOSTNAME="$2"
            shift 2
            ;;
        --tf-token)
            TF_TOKEN="$2"
            shift 2
            ;;
        --tf-organization)
            TF_ORGANIZATION="$2"
            shift 2
            ;;
        --account-id)
            ACCOUNT_ID="$2"
            shift 2
            ;;
        --vcs-id)
            VCS_ID="$2"
            shift 2
            ;;
        --workspaces)
            WORKSPACES="$2"
            shift 2
            ;;
        --skip-workspace-creation)
            SKIP_WORKSPACE_CREATION="true"
            shift
            ;;
        --skip-backend-secrets)
            SKIP_BACKEND_SECRETS="true"
            shift
            ;;
        --lock)
            LOCK="true"
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
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown option: $1"
            usage
            ;;
    esac
done

# Read credentials from Terraform credentials file if not provided
read_tfrc_credentials

# Validate required parameters
if [ -z "$SCALR_TOKEN" ]; then
    echo "Error: Scalr token not found. Please provide it via:"
    echo "  1. --scalr-token argument"
    echo "  2. SCALR_TOKEN environment variable"
    echo "  3. ~/.terraform.d/credentials.tfrc.json file"
    exit 1
fi

if [ -z "$TF_TOKEN" ]; then
    echo "Error: TFC/E token not found. Please provide it via:"
    echo "  1. --tf-token argument"
    echo "  2. TF_TOKEN environment variable"
    echo "  3. ~/.terraform.d/credentials.tfrc.json file"
    exit 1
fi

if [ -z "$TF_ORGANIZATION" ] || [ -z "$ACCOUNT_ID" ]; then
    echo "Error: Required parameters are missing"
    usage
fi

# Build command line arguments for migrator.py
ARGS=(
    "--scalr-hostname" "$SCALR_HOSTNAME"
    "--scalr-token" "$SCALR_TOKEN"
    "--tf-hostname" "$TF_HOSTNAME"
    "--tf-token" "$TF_TOKEN"
    "--tf-organization" "$TF_ORGANIZATION"
    "--account-id" "$ACCOUNT_ID"
    "--workspaces" "$WORKSPACES"
    "--management-env-name" "$MANAGEMENT_ENV_NAME"
    "--management-workspace-name" "$MANAGEMENT_WORKSPACE_NAME"
)

# Add optional parameters if set
if [ -n "$SCALR_ENVIRONMENT" ]; then
    ARGS+=("--scalr-environment" "$SCALR_ENVIRONMENT")
fi

if [ -n "$VCS_ID" ]; then
    ARGS+=("--vcs-id" "$VCS_ID")
fi

if [ "$SKIP_WORKSPACE_CREATION" = "true" ]; then
    ARGS+=("--skip-workspace-creation")
fi

if [ "$SKIP_BACKEND_SECRETS" = "true" ]; then
    ARGS+=("--skip-backend-secrets")
fi

if [ "$LOCK" = "true" ]; then
    ARGS+=("--lock")
fi

pip3 install packaging

# Run migrator.py
echo "Starting migration process..."
python3 migrator.py "${ARGS[@]}"

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