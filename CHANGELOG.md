# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.3.6] - 2026-01-14

### Added

- Added support for migration of sensitive environment variables.

### Changed
- Improved reading of sensitive Terraform variables if the current workspace run did not produce any plan file.

## [0.3.5] - 2025-11-06

### Added

- Cross-platform compatibility for Windows and Linux/macOS systems
- Support for Git Bash, WSL, Cygwin, MSYS2 on Windows and native bash on Unix-like systems
- `--skip-post-migration` option to skip automatic Terraform steps (fmt, init, apply)
- Automatic Python 3.x detection (tries python3.12, python3, python in order)
- Cross-platform virtual environment activation handling
- Graceful handling when `jq` is not available for credential parsing
- Automatic TFC agent pool to Scalr agent pool mapping by name during workspace migration

### Changed

- Enhanced workspace pattern matching with better wildcard support and error handling
- Improved home directory detection for cross-platform credential file access
- More robust command existence checking across different platforms
- Better error messages and fallback behavior for invalid regex patterns
- Agent pool assignment logic now prioritizes TFC workspace-specific agent pools over global configuration

### Fixed

- Regex error "nothing to repeat at position 0" in workspace pattern matching
- Cross-platform path handling for generated Terraform directories
- Virtual environment activation paths for Windows vs Unix systems
- Pattern matching now properly handles shell wildcards (`*`, `?`) and escapes special regex characters

### Technical Improvements

- Added platform detection for Windows (Cygwin, MinGW, MSYS) vs Unix-like systems
- Enhanced pattern cleaning and validation in workspace filtering
- Improved error handling with try-catch blocks and fallback mechanisms
- All shellcheck linting warnings resolved

## [0.3.4] - 2025-04-17

### Bug Fixes

- Processing of shell parameters and handling of missing provider configuration

## [0.3.3] - 2025-04-17

### Enhancements

- Support for agent pool linking via `--agent-pool-name` argument
- Automatic updated of the provider configuration environment access of newly created environments

## [0.3.2] - 2025-04-16

### New Features

- Support for provider configuration linking via `--pc-name` argument
- Improved variable handling with pattern-based skipping

### Improvements

- Updated workspace creation to support provider configuration linking

### Fixes

- Setting workspaces working directories

## [0.3.1] - 2025-04-14

### Changes

- Added proper handling of multiline trigger patterns using heredoc (EOT) format
- Enhanced workspace configuration handling with improved trigger pattern validation

## [0.3.0] - 2025-04-11

### Features

- Terraform code generation of migrated environment, workspaces, and variables.
- Added support for TFC projects to filter workspaces during migration
- Improved virtual environment handling with dependency installation only on first run
- Enhanced credential management with support for `~/.terraform.d/credentials.tfrc.json`
- Improved console output with color-coded messages and clear section headers

### Breaking Changes

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

### Stability Improvements

- Fixed dependency installation to only occur on first run
- Fixed credential file reading to properly handle missing values
- Improved error handling for missing required parameters
- Fixed environment variable handling and default value assignment

### Known Issues

- None at this time

### Migration Path

- No migration required from previous versions as this is the first release

[0.3.5]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.5
[0.3.4]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.4
[0.3.3]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.3
[0.3.2]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.2
[0.3.1]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.1
[0.3.0]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.0
