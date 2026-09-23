# Changelog

All notable changes to this project will be documented in this file.

## [0.5.0] - 2026-09-23

### Added

- `create-provider-configurations.sh` / `create_provider_configurations.py`, an auxiliary script that creates the Scalr
  provider configurations for the cloud accounts used by the TFC/E workspaces, so `migrate.sh --pc-name` has something
  to attach. It recognizes the provider from the workspace environment variables (`ARM_*`, `AWS_*`, `GOOGLE_*`,
  including those inherited from TFC variable sets), maps them to the equivalent Scalr provider configuration
  attributes, derives the credentials type from the variables that exist, groups workspaces by cloud account (
  `ARM_SUBSCRIPTION_ID`, `GOOGLE_PROJECT`, `AWS_ACCOUNT_ID`/`AWS_ROLE_ARN`, or `--key-variable`), and takes the provider
  configuration names from a parameters file or `--name-template`. It writes a workspace → provider configuration map (
  `--map-file`) that a migration loop can read to pass `--pc-name` per workspace. Existing provider configurations are
  reused unless `--update-existing` is passed; `--dry-run`, `--skip-remote-runs`, `--include-unused` and
  `--fail-on-unresolved` support unattended runs.
- Sensitive provider credentials are migrated through a TFC run, the same mechanism the migrator already uses for
  sensitive shell variables: `templates/export-provider-configuration.tf` and
  `templates/migrate-provider-configuration.py` are added to a copy of the workspace configuration, a plan is started in
  TFC, and the in-run script reads the credentials from the run environment — the only place they are readable — and
  creates the provider configuration through the Scalr API. The credentials never reach the machine running the
  migration, one run is started per cloud account (not per workspace), and no run is started when the values are
  readable through the TFC API or the provider configuration already exists. Workspaces using TFC's dynamic (OIDC)
  credentials, or whose credential variables are not defined at all, are reported and skipped, since a run could not
  recover their credentials either.
- Provider configurations are de-duplicated on every axis: workspaces sharing a cloud account share one provider
  configuration and one TFC run, several cloud accounts mapped to the same name in the parameters file write it once per
  execution (the rest reuse it and record `shared_with`), duplicate identifiers in the parameters file are rejected,
  identifiers are matched case-insensitively, and a name that already exists in Scalr is reused rather than recreated.
- Variable sets applied to a workspace are resolved through `GET /workspaces/:id/varsets`, which is what TFC itself
  reports as applied and covers global sets and sets attached to the workspace's project. The organization-wide listing
  is only used as a fallback on TFE versions without that route, since it depends on relationship linkage that is not
  always returned. Variables of a `priority` variable set now override the workspace's own variables, as they do in a
  TFC run, and a workspace reported as having no known provider credentials now says how many variables and which
  variable sets were inspected.
- Parameters-file entries can list several identifiers (`keys`) and can be matched by workspace name (`workspaces`)
  instead of by cloud account identifier. TFC exposes no AWS account id, so AWS accounts are identified by
  `AWS_ACCOUNT_ID`, `AWS_ROLE_ARN` or `AWS_ACCESS_KEY_ID`, and an entry keyed only by the account id could never match;
  the warning for an unmatched identifier now shows the workspaces concerned and the exact entry to add.
- AWS account identity is resolved during the TFC run through `sts:GetCallerIdentity`, signed with SigV4 from the
  standard library (no cloud SDK dependency, and the call needs no IAM permission). TFC stores no AWS account id, so
  without it an account is only identifiable by its access key; parameters-file entries keyed by the account id are now
  matched, and the map file records the real account id. A run is started for this only when the parameters file still
  has an unmatched identifier or `--name-template` is set.
- `--skip-provider-credentials` for `migrate.sh` / `migrator.py`: the TFC variables whose values a provider
  configuration already holds are not migrated into Scalr a second time. The provider configuration script records them
  in the map (`consumed.variables` / `consumed.variable_sets`, by TFC variable id, only for provider configurations that
  exist), and the migration skips those variables in workspaces and in variable sets, including the recovery of their
  sensitive values through a TFC run. A variable set whose variables were all consumed is not created in Scalr at all,
  while one that also carries other variables is migrated without the credentials.
- `migrate.sh` / `migrator.py` read the workspace → provider configuration map written by the provider configuration
  script (`--pc-map`, or `provider-configurations.map.json` in the working directory) and attach the provider
  configuration that belongs to each workspace. One run can now migrate workspaces that use different cloud accounts,
  instead of one run per workspace with `--pc-name`; `--pc-name` still covers the workspaces the map does not name, and
  works on its own without a map. A provider configuration named in the map that no longer exists is reported and the
  workspace is migrated without it, while an explicit `--pc-name` that does not exist still stops the migration.
- Provider configurations are created with no environment access at all (not shared, granted to no environment) instead
  of being shared with every current and future environment. Access is granted by `--scalr-environment`, by
  `environments` in the parameters file, or by the migration itself, which grants its destination environment access to
  every provider configuration it links to a workspace. `"environments": ["*"]` still asks for a shared provider
  configuration explicitly. `--scalr-environment` and environments named by an entry are created when they do not exist
  yet, so the provider configuration script no longer has to run after the migration that creates the environment.
- A provider configuration created by a TFC run is no longer reported as failed. Terraform colorizes its plan output,
  and the escape sequences kept the result of the `external` data source from being parsed, so a run that had created
  the provider configuration was reported as having done nothing. The runs now use `-no-color`, and escape sequences are
  stripped before parsing in any case.
- `aws-account-type` is always sent (`regular` unless the parameters file overrides it with `gov-cloud` or `cn-cloud`):
  the Scalr API requires it for AWS provider configurations although the reference documents it as optional.
- A parameters-file entry matched inside the TFC run now applies in full. The run receives every candidate entry with
  its attributes, parameters, environments and sharing flags, not just its name, so an entry that can only be matched by
  an identity resolved in the run (an AWS account id) is no longer reduced to its name. What the entry declares takes
  precedence over what the migrator infers from the TFC variables.
- API errors now name the field they are about: a JSON:API error's `source.pointer` is appended to its detail (
  `Field required. [/data/attributes/aws-account-type]`) and up to three errors are reported instead of only the first,
  in both the migrator and the in-run script. When Scalr rejects a provider configuration, the in-run script also
  reports which attribute names were sent (names only, never values).
- Attribute names that look right but are not what the API calls them (`aws-access-key-id`, `aws-secret-access-key`,
  `google-credentials-json`, `azurerm-subscription`) are corrected with a warning instead of failing with a 422.
- `provider-configuration.parameters.example.json` documenting the parameters file format.
- `--apply-auto-approve` flag for `migrate.sh`, which runs the post-migration step as `apply -auto-approve -input=false`
  instead of stopping at the interactive `Enter a value:` confirmation. Intended for unattended runs that drive the
  migrator from another script (for example a PowerShell loop over a list of workspaces), where the prompt blocks the
  caller until someone types `yes`. The default behavior is unchanged: without the flag, `apply` still prompts.

### Changed

- Workspace name matching moved from `MigrationService.should_migrate_workspace` to `scalr_tfc_migrate/matching.py` so
  the migrator and the provider configuration script filter `--workspaces` identically. Behavior is unchanged.

### Fixed

- The result of a TFC run was read from the whole plan output, so a `name` or `id` attribute of one of the workspace's
  own resources could be taken for the migration's result: a successful run was then reported as failed, or, if a
  provider configuration happened to carry that name, the wrong one was recorded in the map. Only the
  `scalr_provider_configuration_result` block is read now.
- Granting an environment access to a provider configuration that has no environments relationship at all - the shape
  the provider configuration script now creates - raised `AttributeError` instead of granting it.
- Building the payload of a TFC run described the first workspace of the group, while the run may happen in another one
  when the first has no downloadable configuration version, so the credentials type and the required attributes could
  describe credentials the run does not have.
- Resolving the entries handed to a TFC run created the Scalr environments of parameters-file entries that no workspace
  matches. Environments are now created only for the entry being applied.
- A list rendered over several lines in a generated `main.tf` was read back as the string `[`.
- A second migration into the same directory generated invalid Terraform (`trigger_prefixes = "["workspace-a"]"`, "
  Missing newline after argument") and duplicated every resource it had already written. Reading `main.tf` back
  transformed the resource names a second time (`r_workspace_a` became `r_r_workspace_a`), so no resource was recognized
  as already present: each run appended a copy, and lookups of the environment and of workspaces silently returned
  nothing. The attribute parser also flattened nested blocks such as `vcs_repo` into their parent and turned lists into
  strings, and the block regexp stopped at the first `}`, cutting a resource short at its first nested block. Names read
  from a file are now kept as they are, blocks are matched by balancing braces, and nested blocks, lists and heredocs
  survive the round trip.
- `--pc-name` naming a provider configuration that does not exist raised `IndexError` instead of the intended
  "not found" error: the lookup indexed the first result before testing whether there was one. It now also requires
  an exact name match rather than taking whatever `filter[name]` returned first.

## [0.4.4] - 2026-08-14

### Added

- `--management-workspace-name` flag to set the name of the management workspace that holds the generated Terraform
  code. Previously the name was always derived from the destination environment name (`--scalr-environment`, or the TFC
  project/organization name), which is still the default.
- `--migrate-variable-sets-only` flag to migrate only TFC variable sets, skipping workspaces, state files and workspace
  variables. Intended for splitting a migration into a variable-sets run and a workspaces run; non-global sets are
  linked to the workspaces that already exist in the destination environment, and remaining links are created by the run
  that migrates those workspaces. The flag is rejected together with `--skip-variable-sets` or `--skip-variables="*"`.

### Changed

- Variable set scoping is now expressed in terms of the TFC workspaces "in scope for this run" instead of "migrated in
  this run", so `--migrate-variable-sets-only` scopes non-global sets by the `--workspaces` / `--tfc-project` filters.
  Scoping for regular runs is unchanged.

### Fixed

- `KeyError: 'download'` when migrating sensitive environment variables from a workspace whose latest TFC configuration
  version has no download link (not uploaded, or the archive is no longer available). Older configuration versions are
  now tried, and the migration of that variable/variable set is reported and skipped instead of crashing.
- `TypeError` when downloading a configuration version of a workspace without a working directory (TFC returns `null`
  for workspaces running from the repository root). A working directory that is missing in the downloaded configuration
  is now reported instead of failing later in `terraform init`.
- A failure while recovering sensitive variable set values no longer aborts the rest of that variable set's migration,
  so the non-sensitive variables and the workspace links are still created.
- `--credentials-set-name` help text in `cli.py`, which incorrectly described the flag as skipping variable set
  migration.

### Removed

- Leftover `--skip-workspace-creation` scaffolding in `cli.py` (argparse declaration) and `migrate.sh` (help text and
  flag parsing). The flag was already removed from behavior in 0.4.3, but was still silently accepted by the CLI and
  shell wrapper without doing anything. It's no longer documented or specially parsed; passing it to `migrate.sh` is now
  silently ignored the same way any other unrecognized flag is.

## [0.4.3] - 2026-06-04

### Added

- `--opentofu-version` flag to pin the OpenTofu version used when `--use-opentofu` is enabled (default: latest active
  OpenTofu in Scalr; must be >= 1.6.0).
- `--credentials-set-name` flag to customize the TFC variable set name used for migrator backend credentials during
  sensitive environment variable migration (default: `Scalr-Creds`). That set is still excluded from variable set
  migration to Scalr.

### Removed

- `--skip-workspace-creation` flag and related startup validation in `migrate.sh`.

### Changed

- `--vcs-name` is no longer required at CLI startup; it is required when the migration encounters VCS-driven workspaces.
- OpenTofu migration threshold documented as Terraform >= 1.6.0 (was described as > 1.5.7).
- Migrator credentials variable set name is configurable via `--credentials-set-name` instead of a hardcoded constant (
  `TFC_MIGRATOR_DEFAULT_SECRETS_VARSET_NAME`).

### Breaking Changes

- `--skip-workspace-creation` has been removed. Workspaces are always created if they don't exist in Scalr prior to the
  migration.

## [0.4.2] - 2026-06-03

### Added

- Added `--skip-variable-sets` flag to skip TFC variable set migration while still migrating workspace-level variables.

### Changed

- Post-migration steps (`fmt`, `init`, `apply`) use OpenTofu (`tofu`) when `--use-opentofu` is enabled instead of the
  Terraform CLI.
- Updated README with authentication parameter precedence, `--skip-variable-sets`, and OpenTofu post-migration behavior.

### Fixed

- Command-line arguments and environment variables now take precedence over tokens read from
  `~/.terraform.d/credentials.tfrc.json`.
- Inline `--pc-name=` and `--agent-pool-name=` arguments in `migrate.sh` now map to the correct environment variables.

## [0.4.0] - 2026-05-14

### Added

- Added support for TFC variable set migration to Scalr.
- Added migration of variable set workspace links and environment access updates.
- Added support for sensitive variable set value recovery from plans and TFC runtime environment.

### Changed

- Variable set migration only processes TFC sets that are global, attached to the filtered `--tfc-project`, or attached
  to at least one workspace migrated in this run.
- TFC-global variable sets use Scalr `is-shared` with no explicit environment relationships; non-global sets use
  explicit environment access merged on reruns when a matching set already exists by name.

### Fixed

- Fixed `none is not an allowed value` (HTTP 422) error when creating a workspace whose `auto-apply`, `operations`, or
  `speculative-enabled` attribute is inherited from TFC organization defaults. The migrator now falls back to TFC's
  documented defaults (`auto-apply=false`, `operations=true`, `speculative-enabled=true`) when these fields are null.

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

[0.4.4]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.4.4

[0.4.3]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.4.3

[0.4.2]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.4.2

[0.4.0]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.4.0

[0.3.6]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.6

[0.3.5]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.5

[0.3.4]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.4

[0.3.3]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.3

[0.3.2]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.2

[0.3.1]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.1

[0.3.0]: https://github.com/your-org/terraform-scalr-migrate-tfc/releases/tag/v0.3.0
