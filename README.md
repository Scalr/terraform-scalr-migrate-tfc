# Terraform Cloud/Enterprise to Scalr Migration Tool

This tool helps migrate workspaces from Terraform Cloud/Enterprise (TFC/E) to Scalr. It handles:
- Workspace migration with all attributes
- State file migration
- Variable migration (including sensitive variables from plan files)
- VCS provider configuration
- Provider configuration linking
- Remote state consumers
- Trigger patterns handling
- Workspace locking in TFC/E after migration

## Features

- Migrates workspaces from Terraform Cloud/Enterprise to Scalr
- Preserves workspace configurations, including:
  - VCS settings and trigger patterns
  - Terraform version
  - Execution mode (remote/local)
  - Working directory
  - Auto-apply settings
  - Remote state sharing
  - Variable values (including sensitive ones when available)
- Handles workspace dependencies and remote state consumers
- Generates Terraform configuration for the migrated resources
- Supports workspace filtering using glob patterns
- Automatically configures Terraform credentials
- Creates a management workspace for state management
- Supports project-based workspace filtering
- Properly handles multiline trigger patterns using heredoc (EOT) format
- Preserves state history
- Handles sensitive and non-sensitive variables
- Supports workspace locking
- Creates a management environment and workspace in Scalr
- Generates Terraform resources and import commands
- Supports wildcard workspace selection

## Prerequisites

- Python 3.12 or higher
- Terraform Cloud/Enterprise credentials
- Scalr credentials
- VCS provider configured in Scalr (if migrating workspaces with VCS)
- Provider configuration in Scalr (if linking workspaces to provider configurations)

## Installation

1. Clone this repository:
```bash
git clone https://github.com/your-org/terraform-scalr-migrate-tfc.git
cd terraform-scalr-migrate-tfc
```

2. Make the scripts executable:
```bash
chmod +x migrate.sh
```

## Authentication

The tool supports multiple ways to provide authentication tokens:

### Command line arguments:
```bash
./migrate.sh --tfc-token "your-token" --tfc-organization="my-org" --scalr-hostname "account.scalr.io" --scalr-token "your-token"
```

### Environment variables:
```bash
export SCALR_HOSTNAME="account.scalr.io" # Replace `account` with the actual account name
export SCALR_TOKEN="your-token"
export TFC_TOKEN="your-token"
```

### Terraform credentials file (`~/.terraform.d/credentials.tfrc.json`):

When Scalr hostname is know (via parameter  `--scalr-hostname` or `SCALR_HOSTNAME`), the migrator can read the token from locally cached credentials file (usually written by the `terraform login` command).

```json
{
  "credentials": {
    "account.scalr.io": {
      "token": "your-scalr-token"
    },
    "app.terraform.io": {
      "token": "your-tfc-token"
    }
  }
}
```

To use this auth method run two commands first:

Cache TFC token:

```shell
terraform login
```

Cache Scalr token (replace `account` with the actual account name):
```shell
terraform login account.scalr.io
```

## Usage

```bash
./migrate.sh --scalr-hostname <scalr-hostname> \
             --scalr-token <scalr-token> \
             --tfc-hostname <tfc-hostname> \
             --tfc-token <tfc-token> \
             --tfc-organization <tfc-org> \
             [-v|--vcs-name <vcs-name>] \
             [--pc-name <pc-name>] \
             [--agent-pool-name <agent-pool-name>] \
             [-w|--workspaces <workspace-pattern>] \
             [--skip-workspace-creation] \
             [--skip-backend-secrets] \
             [--skip-tfc-lock] \
             [--management-env-name <name>] \
             [--disable-deletion-protection] \
             [--tfc-project <project-name>] \
             [--skip-variables <pattern>]
```

### Required Arguments

- `--scalr-hostname`: Scalr hostname (e.g., `myorg.scalr.io`)
- `--scalr-token`: Scalr API token
- `--tfc-hostname`: TFC/E hostname (e.g., `app.terraform.io`)
- `--tfc-token`: TFC/E API token
- `--tfc-organization`: TFC/E organization name

### Optional Arguments

- `-v|--vcs-name`: VCS provider name in Scalr (required if not using `--skip-workspace-creation` for VCS driven-workspaces)
- `--pc-name`: Provider configuration name in Scalr to link to workspaces
- `--pc-name`: Agent pool name in Scalr to link to workspaces
- `-w|--workspaces`: Workspace name pattern (supports glob patterns, default: "*")
- `--skip-workspace-creation`: Skip workspace creation in Scalr (use if workspaces already exist)
- `--skip-backend-secrets`: Skip creation of shell variables for backend configuration
- `--skip-tfc-lock`: Skip locking TFC/E workspaces after migration
- `--management-env-name`: Name of the management environment (default: "scalr-admin")
- `--disable-deletion-protection`: Disable deletion protection in workspace resources
- `--tfc-project`: TFC project name to filter workspaces by
- `--skip-variables`: Comma-separated list of variable patterns to skip, or "*" to skip all variables

## Generated Files

The tool generates the following files in the `generated-terraform/$SCALR_ENVIRONMENT` directory:

- `main.tf`: Contains all Terraform resources
- `backend.tf`: Remote backend configuration
- `import_commands.sh`: Script to import resources and push state

### Post-Migration

After successful migration, the tool will execute terraform apply and imports all previously created resources in the management workspace state file.

## Limitations

- Supports up to Terrraform 1.5.7. If a higher version is used, the script will downgrade it to 1.5.7.
- State migration requires at least one state file in the source TFC/E workspace.
- Sensitive terraform variables migration requires at least one plan file in the source TFC/E workspace.
- Sensitive environment variables are not migrated

## Troubleshooting

1. If you encounter authentication errors:
   - Verify your tokens are correct
   - Check the credentials file format
   - Ensure you have the necessary permissions

2. If state migration fails:
   - Check if the workspace has a valid state file
   - Ensure you have sufficient permissions in both platforms

3. If workspace creation fails:
   - Verify the VCS provider is correctly configured
   - Check if the workspace name is available
   - Ensure you have sufficient permissions

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.
