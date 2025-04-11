# Terraform Cloud/Enterprise to Scalr Migration Tool

This tool helps migrate workspaces from Terraform Cloud/Enterprise (TFC/E) to Scalr, including:
- Workspace configurations
- State files
- Variables

## Features

- Migrates workspaces with all their configurations
- Preserves state history
- Handles sensitive and non-sensitive variables
- Supports workspace locking
- Creates a management environment and workspace in Scalr
- Generates Terraform resources and import commands
- Supports wildcard workspace selection

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
chmod +x migrate.sh
```

## Authentication

The tool supports multiple ways to provide authentication tokens:

### Command line arguments:
```bash
./migrate.sh --scalr-hostname "account.scalr.io" --scalr-token "your-token" --tfc-token "your-token"
```

### Environment variables:
```bash
export SCALR_HOSTNAME="account.scalr.io"
export SCALR_TOKEN="your-token"
export TFC_TOKEN="your-token"
./migrate.sh
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

### Basic Usage

```bash
./migrate.sh --tfc-organization "my-org"
```

### Advanced Options

Migrates all production workspaces of dedicated project into separate production Scalr environment:

```bash
./migrate.sh \
  --tfc-organization "my-org" \
  --tfc-project "my-project" \
  --workspaces "prod-*" \
  --scalr-environment "my-project-prod" \
  --management-env-name "scalr-administration"
```

### Available Options

- `--scalr-hostname`: Scalr hostname (default: account.scalr.io)
- `--scalr-token`: Scalr token
- `--scalr-environment`: Scalr environment (optional)
- `--tfc-hostname`: TFC/E hostname (default: app.terraform.io)
- `--tfc-token`: TFC/E token
- `--tfc-organization`: TFC/E organization name
- `--tfc-project`: TFC/E project name
- `--vcs-name`: VCS Provider name
- `--workspaces`: Workspaces to migrate (default: *)
- `--skip-workspace-creation`: Skip workspace creation in Scalr, state migration will be performed only
- `--skip-backend-secrets`: Skip Scalr/TFE secrets creation
- `--skip-tfc-lock`: Skip locking of TFC/E workspaces. By default, workspaces are locked after migration to prevent state conflicts.
- `--management-env-name`: Management environment name in which the generated code will be exported

## Generated Files

The tool generates the following files in the `generated-terraform/$SCALR_ENVIRONMENT` directory:

- `main.tf`: Contains all Terraform resources
- `backend.tf`: Remote backend configuration
- `import_commands.sh`: Script to import resources and push state

### Post-Migration

After successful migration, the tool will execute terraform apply and imports all previously created resources in the management workspace state file.

## Limitations

- Maximum Terraform version is limited to 1.5.7
- State migration requires at least one state file in the source TFC/E workspace.
- Sensitive terraform variables migration requires at least one plan file in the source TFC/E workspace.
- Sensitive environment variables are not migrationed

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
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a new Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.
