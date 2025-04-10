# Terraform Cloud/Enterprise to Scalr Migration Tool

This tool helps migrate workspaces from Terraform Cloud/Enterprise (TFC/E) to Scalr, including:
- Workspace configurations
- State files
- Variables
- VCS connections

## Features

- Migrates workspaces with all their configurations
- Preserves state history
- Handles sensitive and non-sensitive variables
- Supports workspace locking
- Creates a management environment and workspace in Scalr
- Generates Terraform resources and import commands
- Supports wildcard workspace selection
- Handles credentials from multiple sources

## Prerequisites

- Python 3.12
- Terraform CLI
- `jq` command-line tool (for JSON processing)
- Access to both TFC/E and Scalr instances
- Appropriate tokens for both platforms

## Installation

1. Clone this repository:
```bash
git clone https://github.com/your-org/terraform-scalr-migrate-tfc.git
cd terraform-scalr-migrate-tfc
```

2. Make the scripts executable:
```bash
chmod +x migrate.sh post-migration.sh
```

## Authentication

The tool supports multiple ways to provide authentication tokens:

1. Command line arguments:
```bash
./migrate.sh --scalr-token "your-token" --tf-token "your-token"
```

2. Environment variables:
```bash
export SCALR_TOKEN="your-token"
export TF_TOKEN="your-token"
./migrate.sh
```

3. Terraform credentials file (`~/.terraform.d/credentials.tfrc.json`):
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

## Usage

### Basic Usage

```bash
./migrate.sh --tf-organization "my-org" --account-id "my-account"
```

### Advanced Options

```bash
./migrate.sh \
  --scalr-hostname "account.scalr.io" \
  --tf-hostname "app.terraform.io" \
  --tf-organization "my-org" \
  --account-id "my-account" \
  --workspaces "prod-*" \
  --management-env-name "terraform-management" \
  --management-workspace-name "workspace-management"
```

### Available Options

- `--scalr-hostname`: Scalr hostname (default: account.scalr.io)
- `--scalr-token`: Scalr token
- `--scalr-environment`: Scalr environment (optional)
- `--tf-hostname`: TFC/E hostname (default: app.terraform.io)
- `--tf-token`: TFC/E token
- `--tf-organization`: TFC/E organization name
- `--account-id`: Scalr account ID
- `--vcs-id`: VCS identifier
- `--workspaces`: Workspaces to migrate (default: *)
- `--skip-workspace-creation`: Skip workspace creation in Scalr
- `--skip-backend-secrets`: Skip backend secrets creation
- `--lock`: Lock TFE workspaces
- `--management-env-name`: Management environment name
- `--management-workspace-name`: Management workspace name

### Post-Migration

After successful migration, the tool will:
1. Generate Terraform resources in the `generated_terraform` directory
2. Create import commands in `import_commands.sh`
3. Execute post-migration steps if `post-migration.sh` exists

## Generated Files

The tool generates the following files in the `generated_terraform` directory:

- `main.tf`: Contains all Terraform resources
- `backend.tf`: Remote backend configuration
- `import_commands.sh`: Script to import resources and push state

## Limitations

- Maximum Terraform version is limited to 1.5.7
- Workspaces without VCS connections are skipped
- State migration requires at least one successful run in the source workspace

## Troubleshooting

1. If you encounter authentication errors:
   - Verify your tokens are correct
   - Check the credentials file format
   - Ensure you have the necessary permissions

2. If state migration fails:
   - Verify the source workspace has at least one successful run
   - Check if the workspace has a valid state file
   - Ensure you have sufficient permissions in both platforms

3. If workspace creation fails:
   - Verify the VCS provider is correctly configured
   - Check if the workspace name is available
   - Ensure you have sufficient permissions

## Contributing

1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a new Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.
