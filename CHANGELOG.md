[Unreleased]

[0.3.3] - 2025-04-17

### Added
- Support for agent pool linking via `--agent-pool-name` argument
- Automatic updated of the provider configuration environment access of newly created environments

[0.3.2] - 2025-04-16

### Added
- Support for provider configuration linking via `--pc-name` argument
- Improved variable handling with pattern-based skipping

### Changed
- Updated workspace creation to support provider configuration linking

### Fixed
- Setting workspaces working directories

[0.3.1] - 2025-04-14

### Changes

- Added proper handling of multiline trigger patterns using heredoc (EOT) format
- Enhanced workspace configuration handling with improved trigger pattern validation

[0.3.0] - 2025-04-11

### New Features
- Terraform code generation of migrated environment, workspaces, and variables.
- Added support for TFC projects to filter workspaces during migration
- Improved virtual environment handling with dependency installation only on first run
- Enhanced credential management with support for `~/.terraform.d/credentials.tfrc.json`
- Improved console output with color-coded messages and clear section headers

### Changes
- Renamed TFC-related arguments for consistency:
  - `--tf-hostname` → `--tfc-hostname`
  - `--tf-token` → `--tfc-token`
  - `--tf-organization` → `--tfc-organization`
- Renamed lock-related argument:
  - `--lock` → `--skip-tfc-lock`
- Removed `--account-id` requirement as it's no longer needed
- Improved help text with better descriptions and formatting

### Default Values
- `TFC_HOSTNAME` defaults to "app.terraform.io" if not specified
- `SCALR_ENVIRONMENT` defaults to `TFC_PROJECT` or `TFC_ORGANIZATION` if not specified
- `MANAGEMENT_ENV_NAME` defaults to "scalr-admin" if not specified

### Dependencies
- Requires Python 3.12 or higher
- Dependencies are now installed only once when the virtual environment is first created
- Subsequent runs will reuse the existing virtual environment

### Post-Migration Steps
- Automatically navigates to the generated Terraform directory
- Runs `terraform init` and `terraform apply` to complete the migration

### Example Usage
```bash
./migrate.sh \
  --scalr-hostname account.scalr.io \
  --scalr-token your-token \
  --tfc-hostname app.terraform.io \
  --tfc-token your-token \
  --tfc-organization your-org \
  --tfc-project your-project \
  --vcs-name your-vcs
```

### Bug Fixes
- Fixed dependency installation to only occur on first run
- Fixed credential file reading to properly handle missing values
- Improved error handling for missing required parameters
- Fixed environment variable handling and default value assignment

### Known Issues
- None at this time

### Migration Path
- No migration required from previous versions as this is the first release