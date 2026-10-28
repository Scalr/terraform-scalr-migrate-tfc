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
  - Run triggers
  - Team access, as Scalr access policies (with `--team-map`)
- State file migration
  - Preserves state history
- Variable migration (including sensitive variables from plan files)
- Variable set migration
  - Variable set variables (including sensitive values recovery)
  - Non-global variable set workspace links and environment access updates
  - Only TFC variable sets in scope for this run are migrated: **global** sets, sets linked to **`--tfc-project`** (when set), and sets linked to **workspaces in scope for this run** (workspace name patterns). TFC-global sets become Scalr **shared** (`is-shared`) variable sets with **no** explicit environment relationships; non-global sets get environment access merged by name across reruns.
- VCS provider configuration
- Provider configuration linking
- Remote state consumers
- Trigger patterns handling
- Workspace locking in TFC/E after migration to avoid conflicting runs

At the end of the migration, the Scalr Terraform provider code will be generated, allowing you to continue managing Scalr objects with code. A Scalr management environment and workspace will be created for managing Scalr environments and workspaces.

An auxiliary script, [`create-provider-configurations.sh`](#creating-provider-configurations), creates the Scalr provider configurations for the cloud accounts used by the TFC workspaces before the migration runs.

# Usage

## Prerequisites

- Python 3.x (automatically detects python3.12, python3, or python)
- Terraform Cloud/Enterprise credentials
- Scalr credentials
- [VCS provider configured in Scalr](https://docs.scalr.io/docs/vcs-providers) and `--vcs-name` set (only for workspaces that use VCS in TFC/E)
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

Authentication can be provided in three ways. When the same value is set in more than one place, this order applies:

1. **Command-line arguments** (highest priority)
2. **Environment variables**
3. **`~/.terraform.d/credentials.tfrc.json`** (fallback when tokens are not set elsewhere)

Both `--flag value` and `--flag=value` formats are supported for all options (for example, `--tfc-token "your-token"` or `--tfc-token=your-token`).

### Command line arguments:
```bash
./migrate.sh \
  --tfc-token "your-token" \
  --tfc-organization="my-org" \
  --scalr-hostname "account.scalr.io" \
  --scalr-token "your-token"
```

Inline values passed this way are always used, even if tokens for the same hostnames exist in the credentials file.

### Environment variables:
```bash
export SCALR_HOSTNAME="account.scalr.io" # Replace `account` with the actual account name
export SCALR_TOKEN="your-token"
export TFC_TOKEN="your-token"
```

Environment variables are used when a value is not passed on the command line. They take precedence over the credentials file.

### Terraform credentials file (`~/.terraform.d/credentials.tfrc.json`):

When tokens are not provided via the command line or environment variables, the migrator reads them from the locally cached credentials file (usually written by the `terraform login` command). The Scalr hostname must be known (via `--scalr-hostname` or `SCALR_HOSTNAME`) to look up the Scalr token.

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

- `-v|--vcs-name`: VCS provider name in Scalr (required when any workspace in scope is VCS-driven; not checked at startup if omitted)
- `--scalr-environment`: Scalr environment to create (default: `--tfc-project` if set, otherwise `--tfc-organization`)
- `--pc-name`: Provider configuration name in Scalr to link to workspaces, used for the workspaces the provider configuration map does not cover
- `--skip-provider-credentials`: Do not migrate the TFC variables whose values a provider configuration already holds, as recorded in the provider configuration map. A variable set left with nothing else is not created either. Without this flag the credentials end up both in the provider configuration and in Scalr variables
- `--pc-map`: Workspace → provider configuration map written by `create-provider-configurations.sh` (default: `provider-configurations.map.json` when it exists in the working directory). Lets one run attach a different provider configuration to each workspace
- `--agent-pool-name`: Agent pool name in Scalr to link to workspaces
- `-w|--workspaces`: Workspace name pattern (supports shell-style wildcards, default: "*")
  - Examples: `"prod-*"` (starts with prod-), `"*-staging"` (ends with -staging), `"test?"` (test + any single char)
- `--skip-backend-secrets`: Skip creation of shell variables for backend configuration
- `--skip-tfc-lock`: Skip locking TFC/E workspaces after migration
- `--skip-post-migration`: Skip post-migration Terraform/OpenTofu steps (fmt, init, apply)
- `--apply-auto-approve`: Run the post-migration apply as `apply -auto-approve -input=false`, so it does not stop at the interactive approval prompt. Intended for unattended runs (CI, PowerShell/bash loops over several migrations)
- `--skip-variable-sets`: Skip migration of TFC variable sets to Scalr (workspace-level variables are still migrated)
- `--migrate-variable-sets-only`: Migrate **only** TFC variable sets. Workspaces, their state files and variables are not migrated; workspaces that already exist in the destination Scalr environment are reused to link non-global variable sets. Cannot be combined with `--skip-variable-sets` or `--skip-variables="*"`. See [Migrating variable sets separately](#migrating-variable-sets-separately).
- `--management-env-name`: Name of the management environment (default: "scalr-admin")
- `--management-workspace-name`: Name of the management workspace that holds the generated Terraform code (default: the `--scalr-environment` name; spaces are replaced with `-`)
- `--disable-deletion-protection`: Disable deletion protection in workspace resources
- `--tfc-project`: TFC project name to filter workspaces by
- `--skip-variables`: Comma-separated list of variable key patterns to skip, or `"*"` to skip all variable migration (including variable sets)
- `--use-opentofu`: Use OpenTofu for workspaces with Terraform version >= 1.6.0 instead of downgrading to 1.5.7
- `--opentofu-version`: OpenTofu version to use when `--use-opentofu` is set (must be >= 1.6.0; default: latest active OpenTofu version in Scalr)
- `--credentials-set-name`: Name of the TFC variable set the migrator creates for backend/credential secrets during sensitive environment variable migration (default: `Scalr-Creds`). This set is skipped when migrating variable sets to Scalr.
- `--workspaces-file`: File with the exact names of the workspaces to migrate, one per line (`#` starts a comment). Combined with `-w`, a workspace must match both. See [Migrating in waves](#migrating-in-waves)
- `--team-map`: JSON file mapping TFC/E teams and access levels to Scalr teams and roles; when set, team access is migrated to Scalr access policies. See [Team access](#team-access)
- `--skip-run-triggers`: Do not migrate TFC/E run triggers
- `--layout`: `single` (default) writes the generated code to `main.tf`; `per-workspace` writes each workspace with its variables, access policies and run triggers to its own `ws_<name>.tf`
- `--report-file`: Where to write the JSON report of the run (default: `generated-terraform/<environment>/reports/<mode>-<timestamp>.json`)

Read-only modes, which skip the post-migration steps:

- `--inventory`: Export the TFC/E workspaces in scope, their team access, remote state consumers and run triggers to `--inventory-dir` (default: `migration-inventory`). Needs TFC/E credentials only
- `--dry-run`: Report what a migration with the same arguments would do, without changing anything in TFC/E or Scalr
- `--verify`: Check workspaces migrated by earlier runs against TFC/E. `--verify-plan` also queues a plan-only run in each VCS-driven Scalr workspace (this creates runs)

### Exit codes

- `0`: success
- `1`: the run stopped (invalid input, API error), `--dry-run` found blockers, or `--verify` found mismatches
- `2`: the migration completed but some workspaces failed; the report names them and the error. `migrate.sh` still runs the post-migration steps so that what was created is imported, then exits with `2`

## Migrating in waves

Large organizations are easier to migrate in batches: a pilot of a few workspaces first, then larger groups as the process proves itself. Every step below reads and writes files, so a person, a CI pipeline or an AI agent can drive it and review it.

```bash
# 1. Inventory: what exists in TFC/E, who has access to what, who reads whose state, which runs trigger which
./migrate.sh --tfc-organization my-org --inventory

# 2. Write the wave, one workspace per line, from migration-inventory/*.csv
cat > wave-1.txt <<'EOF'
# pilot: leaf workspaces, no consumers
network-sandbox
dns-sandbox
EOF

# 3. Preview it: what would be created, reused or left pending, and what blocks it
./migrate.sh --tfc-organization my-org --scalr-environment my-env --workspaces-file wave-1.txt --dry-run

# 4. Migrate it
./migrate.sh --tfc-organization my-org --scalr-environment my-env --workspaces-file wave-1.txt \
  --team-map team-map.json --layout per-workspace --apply-auto-approve

# 5. Check it
./migrate.sh --tfc-organization my-org --scalr-environment my-env --workspaces-file wave-1.txt --verify
```

### What each step writes

- `migration-inventory/` (`--inventory`): `workspaces.csv`, `team-access.csv`, `state-consumers.csv`, `run-triggers.csv`, `inventory.json`, and `team-map.example.json`, a skeleton of the team map listing every team and access level found. Keys of variables are listed, values never are.
- `generated-terraform/<environment>/reports/` (every other run): `plan-*.json` for `--dry-run`, `migration-*.json` for a migration, `verify-*.json` for `--verify`. One entry per workspace in scope with its status and, for a migration, what happened to its state, each variable key, provider configuration, agent pool, remote state consumers, access policies, run triggers and TFC/E lock. Values of variables and tokens are not written.
- `generated-terraform/workspace-map.json`: TFC/E workspace → Scalr hostname, environment id and workspace id, merged across all runs and environments. It is how a wave finds the workspaces of earlier waves, and the reference for repointing `terraform_remote_state` and `tfe_outputs` readers at Scalr.

### Dependencies between waves

Remote state consumers and run triggers often connect workspaces of different waves. The migrator handles both directions:

- When a workspace is migrated, its consumers that are already in Scalr (this run or an earlier wave) are allowed to read its state, and the workspaces it consumes from earlier waves get it added as a consumer.
- A run trigger is created as soon as both workspaces are in Scalr; until then it is reported as `pending`, and the run that migrates the other workspace creates it.

The Scalr API cannot list run triggers, so a re-run recognizes a trigger it created by its `scalr_run_trigger` block in the generated code of the same environment.

Migrating a workspace locks it in TFC/E, so its TFC/E state stops changing. Consumers that still read it from TFC/E keep reading the last outputs until their `terraform_remote_state` (or `tfe_outputs`, which becomes Scalr's `scalr_outputs`) points at Scalr; plan the code change for the consumers together with the wave of the workspace they read from.

## Team access

`--team-map FILE` migrates the TFC/E team access of each workspace to Scalr [access policies](https://docs.scalr.io/docs/identity-and-access-management). Nothing is guessed: every TFC/E team and access level must be mapped explicitly, and `--inventory` writes a skeleton with all of them.

```json
{
  "teams": {
    "platform-admins": "platform-admins",
    "app-team": "Application Team"
  },
  "access_levels": {
    "read": ["reader"],
    "plan": ["reader"],
    "write": ["user"],
    "admin": ["admin"],
    "custom": ["user"]
  },
  "project_access_levels": {
    "maintain": ["user"]
  },
  "project_access": "ignore",
  "overrides": [
    {"team": "app-team", "workspace": "app-*", "roles": ["user"]}
  ]
}
```

- `teams`: TFC/E team name → name of an existing Scalr team (teams are usually synchronized from the identity provider, so the migrator does not create them).
- `access_levels`: TFC/E workspace access level (`read`, `plan`, `write`, `admin`, `custom`) → Scalr role names.
- `overrides`: roles for a team on the workspaces matching `workspace` (a pattern, as `-w` takes), checked before `access_levels`.
- `project_access`: what TFC/E project-level team access becomes — `environment` (one environment access policy per team; requires `--tfc-project`), `workspaces` (a workspace access policy on each workspace of the project), or `ignore` (default). Project access levels (`read`, `write`, `maintain`, `admin`, `custom`) map through `project_access_levels`.

A team or access level that is not mapped, or a team or role that does not exist in Scalr, is reported and gets no policy. When the team already has an access policy on the workspace, it is left unchanged — reported as `exists`, or `exists-with-other-roles` when its roles differ. Policies the migrator creates are written to the generated code as `scalr_access_policy` resources.

The TFC/E token needs permission to read team access (an organization owner token, or one with "Manage Teams" / "Manage Workspaces").

## Migrating variable sets separately

The migration is idempotent, so variable sets and workspaces can be migrated in separate runs:

```bash
# 1st run: variable sets only
./migrate.sh --tfc-organization "my-org" --scalr-environment "my-env" --migrate-variable-sets-only

# 2nd run: workspaces (variable sets already exist and are updated in place and linked)
./migrate.sh --tfc-organization "my-org" --scalr-environment "my-env"
```

In `--migrate-variable-sets-only` mode:

- TFC workspaces are still listed and filtered by `--workspaces` / `--tfc-project` to determine which variable sets are in scope, but no workspace, state file or workspace variable is migrated and no TFC workspace is locked.
- Non-global variable sets are linked only to workspaces that already exist in the destination Scalr environment (from a previous run). Workspaces that do not exist yet are reported, and the links are created on the run that migrates them.
- The management environment/workspace and the generated Terraform code are still created, unless `--skip-post-migration` is set.

## Creating provider configurations

`create-provider-configurations.sh` (`create_provider_configurations.py`) is an auxiliary script that creates the Scalr [provider configurations](https://docs.scalr.io/docs/provider-configurations) for the cloud accounts used by the TFC/E workspaces, so that `migrate.sh --pc-name` has something to attach. It calls only the TFC and Scalr REST APIs — no cloud provider API is involved.

What it does:

1. Lists the TFC/E workspaces of the organization (respecting `--tfc-project` and `-w/--workspaces`).
2. Reads the environment variables of each workspace — its own, plus the variable sets TFC reports as applied to it (`GET /workspaces/:id/varsets`, which covers the global sets of the organization and the sets attached to the workspace's project; a `priority` set overrides the workspace's own variables) and recognizes the provider from them: `ARM_*` → `azurerm`, `AWS_*` → `aws`, `GOOGLE_*` → `google`. Each known variable maps to the equivalent Scalr provider configuration attribute, and the credentials type is derived from which variables exist (`client-secrets`, `access_keys`, `role_delegation`, `service-account-key`).
3. Groups the workspaces by the cloud account they use — `ARM_SUBSCRIPTION_ID`, `GOOGLE_PROJECT`, `AWS_ACCOUNT_ID`/`AWS_ROLE_ARN`, or the variable given with `--key-variable` — so one provider configuration is created per account, not per workspace.
4. Resolves the provider configuration name for each account from the parameters file (`--parameters-file`, default `provider-configuration.parameters.json`) or from `--name-template`.
5. Creates the provider configurations that do not exist yet and writes a workspace → provider configuration map (`--map-file`, default `provider-configurations.map.json`).

```bash
./create-provider-configurations.sh \
  --scalr-hostname "account.scalr.io" \
  --tfc-organization "my-org" \
  --parameters-file provider-configuration.parameters.json
```

Credentials are taken from `$SCALR_HOSTNAME`, `$SCALR_TOKEN`, `$TFC_HOSTNAME`, `$TFC_TOKEN` and `$TFC_ORGANIZATION` when the corresponding arguments are omitted, and from `~/.terraform.d/credentials.tfrc.json` like `migrate.sh` does.

The script is idempotent: a provider configuration whose name already exists is reused and left untouched (`--update-existing` refreshes its credentials instead), so re-runs only create what is missing. `--dry-run` reports what a run would do without changing anything, and `--fail-on-unresolved` exits non-zero when some in-scope workspace could not be mapped.

### How sensitive credentials are migrated

TFC never returns the value of a sensitive variable through its API, and provider credentials are almost always sensitive — so a script running on your machine cannot read `ARM_CLIENT_SECRET`, `AWS_SECRET_ACCESS_KEY` or `GOOGLE_CREDENTIALS`. They are available in the environment of a TFC run, which is where the migrator already recovers sensitive shell variables, and the same mechanism is used here:

1. The TFC variable set holding the Scalr credentials (`--credentials-set-name`, default `Scalr-Creds`) is created, so TFC runs can reach the Scalr API.
2. The current configuration version of one workspace of the group is downloaded, and an `external` data source ([templates/export-provider-configuration.tf](templates/export-provider-configuration.tf)) plus the script it calls ([templates/migrate-provider-configuration.py](templates/migrate-provider-configuration.py)) are added to it.
3. A plan is started in that TFC workspace. While the plan runs, the script reads the provider credentials from the run environment, maps them to the Scalr attributes and creates the provider configuration through the Scalr API. The credentials never reach the machine running the migration, and the payload sent to TFC contains no secret.
4. Scalr is then queried to confirm the provider configuration exists. The plan is speculative and its result does not matter — it may fail on the workspace's own configuration without affecting the migration.

Consequences worth knowing:

- One TFC run is started per cloud account that needs a sensitive value. Workspaces that share an account share the run, and cloud accounts that the parameters file maps to the same provider configuration name share it too: the name is written once per execution, and the other accounts reuse it (reported as `shared_with` in the map file). A provider configuration that already exists is never written again unless `--update-existing` is passed.
- No run is started when everything the provider needs is readable through the TFC API (for example a `google` workspace whose credentials are not marked sensitive), or when the provider configuration already exists. `--skip-remote-runs` disables the TFC runs entirely and only creates what can be created directly.
- `terraform` (or `tofu`, via `--terraform-binary tofu`) must be installed locally, and the workspace must have a downloadable configuration version.
- Workspaces authenticating with dynamic credentials (TFC's OIDC integration: `ARM_USE_OIDC`, `TFC_AWS_PROVIDER_AUTH`, `TFC_GCP_PROVIDER_AUTH`, ...) have no static credential to copy. They are reported and skipped; configure OIDC on the Scalr provider configuration instead.

### Parameters file

See [provider-configuration.parameters.example.json](provider-configuration.parameters.example.json). The file maps a cloud account identifier to the name of the provider configuration to create; credentials do not belong in it, they come from TFC:

```json
{
  "defaults": {"export_shell_variables": true},
  "provider_configurations": [
    {"subscription_id": "00000000-...-0001", "name": "azure-production", "environments": ["production"]},
    {"subscription_id": "00000000-...-0002", "name": "azure-staging", "environments": ["staging"]},
    {"project": "my-gcp-project", "name": "gcp-production"}
  ]
}
```

- The identifier key may be `key`, `subscription_id`, `account_id` or `project`, and the name key `name` or `provider_configuration`. Identifiers are matched case-insensitively, `defaults` apply to every entry, and a plain JSON list of entries is also accepted.
- An entry can declare several identifiers with `keys`, and can be matched by workspace instead with `workspaces`. Both help when TFC carries no account identifier of its own: AWS workspaces are identified by `AWS_ACCOUNT_ID`, `AWS_ROLE_ARN` or, failing those, `AWS_ACCESS_KEY_ID`. `${key}` resolves to whichever identifier was used.
- For AWS, an entry keyed by the **account id** is matched during the TFC run: the in-run script calls `sts:GetCallerIdentity` with the credentials of the run to find out which account they belong to, and matches the parameters file on that. `GetCallerIdentity` requires no IAM permission, and the call is signed with SigV4 from the standard library, so no cloud SDK is added as a dependency. The account id then appears as `key` in the map file instead of the access key. With `--skip-remote-runs` there is no run, so an account-id-keyed entry cannot be matched.
- `environments` accepts a list of Scalr environment names, or `["*"]` for a provider configuration shared with every environment of the account. `is_shared`, `export_shell_variables` and `is_custom` map to the API attributes of the same name.
- Entries are applied in full whether they are matched locally or inside the TFC run, and what an entry declares wins over what the migrator infers from the TFC variables.
- `environments` lists the Scalr environments that may use the provider configuration. Without it, and without `--scalr-environment`, the provider configuration reaches no environment until a migration links it to a workspace and grants its own environment access.

### Environment access

Provider configurations are created with **no environment access**: not shared, and granted to no environment. Access is added deliberately, one environment at a time, in one of three ways — the first two create the environment if it does not exist yet, so the order of the two scripts does not matter:

```bash
# all provider configurations of this run are usable only from "my-env"
./create-provider-configurations.sh --tfc-organization "my-org" --scalr-environment "my-env"
```

```json
{"subscription_id": "00000000-...-0001", "name": "azure-production", "environments": ["prod", "prod-dr"]}
```

The entry wins over the flag, and `"environments": ["*"]` asks for a shared provider configuration explicitly — available to every current and future environment of the account.

The third way needs nothing at all: `migrate.sh` **grants its destination environment access** to every provider configuration it links to a workspace, when that provider configuration is not shared. So a provider configuration created with no access is not stranded — each migration adds the environment it needs, and nothing else. It also means a provider configuration created without `--scalr-environment` is unusable until a migration (or a Scalr user) grants it access, which is the point: nothing is reachable that nobody asked for.
- `attributes` overrides or adds Scalr API attributes (`aws-account-type`, `azurerm-audience`, ...). Values support `${key}` (the account identifier) and `${env:VAR}` (an environment variable of the machine running the script), so a value that is not in TFC at all can still be supplied without writing it into the file.
- `--name-template`, e.g. `--name-template "{provider}-{key}"`, names the accounts the file does not cover and makes the file optional altogether.
- Entries no workspace uses are ignored unless `--include-unused` is passed; creating those requires `provider_name` and `attributes` in the entry, since there is no TFC workspace to read from.

### Recommended order

```bash
# 1. provider configurations for the cloud accounts the workspaces use, scoped to the destination environment
./create-provider-configurations.sh \
  --scalr-hostname "account.scalr.io" --tfc-organization "my-org" \
  --scalr-environment "my-env" --parameters-file provider-configuration.parameters.json

# 2. migrate the workspaces; each one gets the provider configuration the map names for it
./migrate.sh \
  --scalr-hostname "account.scalr.io" --tfc-organization "my-org" \
  --scalr-environment "my-env" --skip-provider-credentials --apply-auto-approve
```

Step 2 reads `provider-configurations.map.json` from the working directory (or `--pc-map FILE`) and links the provider configuration that belongs to each workspace, so one run can migrate workspaces that use different cloud accounts. `--pc-name` is only needed for the workspaces the map does not cover, and works on its own for a migration without a map.

`--skip-provider-credentials` then leaves the credentials themselves behind in TFC: the map records which TFC variables a provider configuration now holds, and those are not copied into Scalr as workspace variables or variable set variables. Without it the same credentials exist twice, once in the provider configuration and once as (sensitive) shell variables.

Step 1 is idempotent and can be re-run: existing provider configurations are reused, and only what is missing is created. Step 2 also writes the provider configuration into the generated Terraform as `provider_configuration { id = ... }`, and grants the environment access to it when it is not shared.

### Using the map during migration

The map file tells the migration loop which provider configuration belongs to each workspace:

```json
{
  "workspaces": {
    "app-prod": {
      "provider": "azurerm",
      "key_variable": "ARM_SUBSCRIPTION_ID",
      "key": "00000000-...-0001",
      "provider_configuration": "azure-production",
      "status": "created"
    }
  },
  "provider_configurations": {
    "azurerm:00000000-...-0001": {"name": "azure-production", "id": "pcfg-xxx", "status": "created", "method": "tfc-run"}
  },
  "skipped_workspaces": [],
  "consumed": {
    "variables": {"var-xxx": {"key": "AWS_SECRET_ACCESS_KEY", "provider_configuration": "aws-development-test"}},
    "variable_sets": {"varset-xxx": {"name": "aws-credentials", "provider_configuration": "aws-development-test"}}
  }
}
```

`consumed` lists the TFC variables whose values ended up in a provider configuration; it is what `migrate.sh --skip-provider-credentials` uses, and it is only filled for provider configurations that exist in Scalr.

`migrate.sh` reads this file itself, so a loop passing `--pc-name` per workspace is no longer needed: a single run migrates every workspace with the provider configuration that belongs to it. Reading the map from a script is still useful to see what will be attached, or to catch workspaces that were not mapped:

```powershell
$map = Get-Content provider-configurations.map.json | ConvertFrom-Json
$map.workspaces.PSObject.Properties |
    Where-Object { -not $_.Value.provider_configuration } |
    ForEach-Object { Write-Warning "No provider configuration for $($_.Name)" }
```

A provider configuration named in the map that no longer exists in Scalr is reported and the workspace is migrated without it; an explicit `--pc-name` that does not exist still stops the migration.

## Generated Files

The tool generates the following files in the `generated-terraform/$SCALR_ENVIRONMENT` directory so you can manage your workspaces with the Scalr Terraform provider:

- `main.tf`: Contains all Terraform resources (with `--layout per-workspace`, the shared ones; each workspace is in its own `ws_<name>.tf`)
- `backend.tf`: Remote backend configuration
- `imports.tf`: Import blocks for the resources the run created
- `reports/`: JSON reports of the runs, see [What each step writes](#what-each-step-writes)

### Post-Migration

After successful migration, the tool automatically runs the following steps (unless `--skip-post-migration` is specified):

1. Navigate to the generated Terraform directory (`generated-terraform/$SCALR_ENVIRONMENT`)
2. Run `fmt` to format the generated code
3. Run `init` to initialize the workspace
4. Run `apply` to import all previously created resources into the management workspace state file

By default these steps use the `terraform` CLI. When `--use-opentofu` is enabled, they use `tofu` instead (`tofu fmt`, `tofu init`, `tofu apply`).

The `apply` step asks for the usual interactive confirmation. When the migration is driven by another script and nobody is there to type `yes`, pass `--apply-auto-approve` to run `apply -auto-approve -input=false` instead:

```bash
./migrate.sh --scalr-hostname account.scalr.io --tfc-organization org --use-opentofu --apply-auto-approve
```

`-input=false` is added together with `-auto-approve` so that a missing input fails the run instead of blocking on another prompt.

To skip these automatic steps and run them manually, use the `--skip-post-migration` flag.

## Limitations

- By default, supports up to Terraform 1.5.7. If a higher version is used, the script will downgrade it to 1.5.7.
- When `--use-opentofu` is enabled, workspaces with Terraform version >= 1.6.0 use OpenTofu (latest active version in Scalr, or the version from `--opentofu-version`) instead of downgrading. This requires Scalr to support OpenTofu.
- State migration requires at least one state file in the source TFC/E workspace.
- Sensitive terraform variables migration requires at least one plan file in the source TFC/E workspace.
- Sensitive environment variables requires triggering of the remote run in a TFC/E workspace
- Migrating provider credentials with `create-provider-configurations.sh` needs a TFC run per cloud account whose
  credentials are sensitive, so the workspace must have a downloadable configuration version, and `terraform` (or
  `tofu`) must be installed locally. Workspaces authenticating with TFC's dynamic (OIDC) credentials have no static
  credential to copy and are reported instead
- The AWS account id is not stored by TFC; it is resolved during the TFC run with `sts:GetCallerIdentity`. Without a
  run (`--skip-remote-runs`), AWS accounts are identified by `AWS_ACCOUNT_ID`, `AWS_ROLE_ARN` or `AWS_ACCESS_KEY_ID`

## Troubleshooting

1. If you encounter authentication errors:
   - Verify your tokens are correct
   - Check that command-line tokens are not being overridden (CLI arguments take priority over the credentials file)
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
