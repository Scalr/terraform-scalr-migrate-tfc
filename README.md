# TFC to Scalr Migration Overview

This script will migrate the following objects from TFC to Scalr in bulk:
- Workspaces with all attributes
  - VCS settings and trigger patterns
  - Terraform version
  - Execution mode (remote/local)
  - Working directory
  - Auto-apply settings
  - Remote state sharing
  - Variable values (including sensitive variables when available)
  - Workspace dependencies
- State file migration
  - Preserves state history
- Variable migration (including sensitive variables from plan files)
- VCS provider configuration
- Provider configuration linking
- Remote state consumers
- Trigger patterns handling
- Workspace locking in TFC/E after migration to avoid conflicting runs

At the end of the migration, the Scalr Terraform provider code will be generated, allowing you to continue managing Scalr objects with code. A Scalr management environment and workspace will be created for managing Scalr environments and workspaces.

# Usage

## Prerequisites

- Python 3.x (automatically detects python3.12, python3, or python)
- Terraform Cloud/Enterprise credentials
- Scalr credentials
- [VCS provider configured in Scalr](https://docs.scalr.io/docs/vcs-providers) (if migrating workspaces with VCS)
- [Provider configuration in Scalr](https://docs.scalr.io/docs/provider-configurations) (if linking workspaces to provider configurations)

## Cross-Platform Compatibility

This migration tool is designed to work seamlessly across different operating systems and environments:

### Supported Platforms
- **Linux/macOS**: Native bash environments
- **Windows**: Git Bash, WSL (Windows Subsystem for Linux), Cygwin, MSYS2

### Automatic Detection
- **Python**: Automatically detects and uses the best available Python 3.x installation
- **Operating System**: Automatically adapts paths and commands based on the detected platform
- **Virtual Environment**: Handles activation scripts for both Windows and Unix-like systems
- **Home Directory**: Cross-platform detection for credential file locations

### Dependencies
- **jq**: Optional for reading Terraform credentials file (graceful fallback if not available)
- **bash**: Required shell environment (available on all supported platforms)

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

Authentication can be performed through the command line by setting the credentials as environment variables or in the Terraform credentials file.

### Command line arguments:
Note: The Scalr and TFC tokens can be set as environment variables (see below)
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

When the Scalr hostname is known (via parameter `--scalr-hostname` or `SCALR_HOSTNAME`), the migrator can read the token from the locally cached credentials file (usually written by the `terraform login` command).

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

To use this auth method, run two commands first:

Cache TFC token:

```shell
terraform login
```

Cache Scalr token (replace `account` with the actual account name):
```shell
terraform login account.scalr.io
```

## Execution

```bash
./migrate.sh --tfc-token "your-token" --tfc-organization="my-org" --scalr-hostname "your-account.scalr.io" --scalr-token "your-token"
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
- `--agent-pool-name`: Agent pool name in Scalr to link to workspaces
- `-w|--workspaces`: Workspace name pattern (supports shell-style wildcards, default: "*")
  - Examples: `"prod-*"` (starts with prod-), `"*-staging"` (ends with -staging), `"test?"` (test + any single char)
- `--skip-workspace-creation`: Skip workspace creation in Scalr (use if workspaces already exist)
- `--skip-backend-secrets`: Skip creation of shell variables for backend configuration
- `--skip-tfc-lock`: Skip locking TFC/E workspaces after migration
- `--skip-post-migration`: Skip post-migration Terraform steps (fmt, init, apply)
- `--management-env-name`: Name of the management environment (default: "scalr-admin")
- `--disable-deletion-protection`: Disable deletion protection in workspace resources
- `--tfc-project`: TFC project name to filter workspaces by
- `--skip-variables`: Comma-separated list of variable patterns to skip, or "*" to skip all variables

## Generated Files

The tool generates the following files in the `generated-terraform/$SCALR_ENVIRONMENT` directory so you can manage your workspaces with the Scalr Terraform provider:

- `main.tf`: Contains all Terraform resources
- `backend.tf`: Remote backend configuration
- `import_commands.sh`: Script to import resources and push state

### Post-Migration

After successful migration, the tool will automatically execute the following steps (unless `--skip-post-migration` is specified):
1. Navigate to the generated Terraform directory
2. Run `terraform fmt` to format the generated code
3. Run `terraform init` to initialize the workspace
4. Run `terraform apply` to import all previously created resources in the management workspace state file

To skip these automatic steps and run them manually, use the `--skip-post-migration` flag.

## Limitations

- Supports up to Terrraform 1.5.7. If a higher version is used, the script will downgrade it to 1.5.7.
- State migration requires at least one state file in the source TFC/E workspace.
- Sensitive terraform variables migration requires at least one plan file in the source TFC/E workspace.
- Sensitive environment variables requires triggering of the remote run in a TFC/E workspace

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
