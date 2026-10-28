"""Command-line entrypoint."""
import argparse
import sys
import traceback

from scalr_tfc_migrate import constants
from scalr_tfc_migrate.args import MigratorArgs
from scalr_tfc_migrate.console import ConsoleOutput
from scalr_tfc_migrate.constants import DEFAULT_MANAGEMENT_ENV_NAME
from scalr_tfc_migrate.errors import APIError, InvalidInputError
from scalr_tfc_migrate.inventory import InventoryExporter
from scalr_tfc_migrate.planner import MigrationPlanner
from scalr_tfc_migrate.service import MigrationService
from scalr_tfc_migrate.verify import MigrationVerifier


def main():
    parser = argparse.ArgumentParser(description='Migrate workspaces from TFC/E to Scalr')
    parser.add_argument('--scalr-hostname', type=str, help='Scalr hostname')
    parser.add_argument('--scalr-token', type=str, help='Scalr token')
    parser.add_argument('--scalr-environment', type=str,
                        help='Optional. Scalr environment to create. By default it takes TFC/E organization name.')
    parser.add_argument('--tfc-hostname', type=str, help='TFC/E hostname')
    parser.add_argument('--tfc-token', type=str, help='TFC/E token')
    parser.add_argument('--tfc-organization', type=str, help='TFC/E organization name')
    parser.add_argument('-v', '--vcs-name', type=str, help='VCS identifier')
    parser.add_argument('--pc-name', type=str,
                        help='Provider configuration name, used for every workspace this run creates that the '
                             'provider configuration map does not cover')
    parser.add_argument('--pc-map', type=str,
                        help='Workspace → provider configuration map written by create_provider_configurations.py. '
                             f'Default: {constants.DEFAULT_PC_MAP_FILE} when it exists')
    parser.add_argument('--skip-provider-credentials', action='store_true',
                        help='Do not migrate the TFC variables whose values a provider configuration already '
                             'holds, according to the provider configuration map. A variable set left with '
                             'nothing else is not created')
    parser.add_argument('--agent-pool-name', type=str, help='Scalr agent pool name')
    parser.add_argument('-w', '--workspaces', type=str, help='Workspaces to migrate. By default - all')
    parser.add_argument('--workspaces-file', type=str,
                        help='File with the exact names of the workspaces to migrate, one per line (a migration '
                             'wave). "#" starts a comment. Combined with --workspaces, a workspace must match both')
    parser.add_argument('--skip-backend-secrets', action='store_true',
                        help='Whether to create shell variables (`SCALR_` and `TFC_`) in Scalr.')
    parser.add_argument('--skip-tfc-lock', action='store_true', help='Whether to skip locking of TFC/E workspaces')
    parser.add_argument('--management-env-name', type=str, default=DEFAULT_MANAGEMENT_ENV_NAME,
                        help=f'Name of the management environment. Default: {DEFAULT_MANAGEMENT_ENV_NAME}')
    parser.add_argument('--management-workspace-name', type=str,
                        help='Name of the management workspace that holds the generated Terraform code. '
                             'Default: the destination Scalr environment name.')
    parser.add_argument('--disable-deletion-protection', action='store_true',
                        help='Disable deletion protection in workspace resources. Default: enabled')
    parser.add_argument('--tfc-project', type=str, help='TFC project name to filter workspaces by')
    parser.add_argument('--skip-variables', type=str,
                        help='Comma-separated list of variable keys to skip, or "*" to skip all variables')
    parser.add_argument('--use-opentofu', action='store_true',
                        help='Use OpenTofu for workspaces with Terraform version > 1.5.7 instead of downgrading')
    parser.add_argument('--opentofu-version',  type=str,
                        help='If Opentofu is used, a version to migrate workspaces with Terraform => 1.6.0. Default: latest Opentofu version')
    parser.add_argument('--skip-post-migration', action='store_true', help='Whether to skip post-migrate actions')
    parser.add_argument('--skip-variable-sets', action='store_true',
                        help='Skip migration of TFC variable sets to Scalr')
    parser.add_argument('--migrate-variable-sets-only', action='store_true',
                        help='Migrate only TFC variable sets. Workspaces, their states and variables are not '
                             'migrated, existing Scalr workspaces are reused for variable set links.')
    parser.add_argument('--credentials-set-name', type=str,
                        help='Name of the TFC variable set holding the Scalr credentials used to migrate sensitive '
                             'environment variables. Default: '
                             f'{constants.TFC_MIGRATOR_DEFAULT_SECRETS_VARSET_NAME}')
    parser.add_argument('--report-file', type=str,
                        help='Where to write the JSON report of the run. Default: '
                             'generated-terraform/<environment>/reports/<mode>-<timestamp>.json')
    parser.add_argument('--inventory', action='store_true',
                        help='Export the TFC/E workspaces in scope, their team access, remote state consumers and '
                             'run triggers to CSV and JSON files, without migrating anything. Needs TFC/E '
                             'credentials only')
    parser.add_argument('--inventory-dir', type=str,
                        help=f'Directory for the --inventory files. Default: {constants.DEFAULT_INVENTORY_DIR}')
    parser.add_argument('--dry-run', action='store_true',
                        help='Report what a migration with the same arguments would create, reuse or skip, and '
                             'what would block it, without changing anything in TFC/E or Scalr')
    parser.add_argument('--team-map', type=str,
                        help='JSON file mapping TFC/E teams and access levels to Scalr teams and roles. When set, '
                             'TFC/E team access is migrated to Scalr access policies')
    parser.add_argument('--skip-run-triggers', action='store_true',
                        help='Do not migrate TFC/E run triggers between workspaces')
    parser.add_argument('--layout', type=str, choices=[constants.LAYOUT_SINGLE, constants.LAYOUT_PER_WORKSPACE],
                        help='Layout of the generated Terraform code: everything in main.tf (single, default), or '
                             'one ws_<name>.tf file per workspace (per-workspace)')
    parser.add_argument('--verify', action='store_true',
                        help='Check workspaces migrated by earlier runs against TFC/E (state serial, variables, '
                             'access policies, lock) without changing anything')
    parser.add_argument('--verify-plan', action='store_true',
                        help='With --verify: also queue a plan-only run in each Scalr workspace and report '
                             'whether it proposes changes')

    args = parser.parse_args()

    modes = [flag for flag, enabled in (
        ('--inventory', args.inventory),
        ('--dry-run', args.dry_run),
        ('--verify', args.verify or args.verify_plan),
    ) if enabled]
    if len(modes) > 1:
        ConsoleOutput.error(f"{' and '.join(modes)} cannot be used together")
        sys.exit(1)

    if args.inventory:
        required_args = ['tfc_hostname', 'tfc_token', 'tfc_organization']
    else:
        required_args = ['scalr_hostname', 'scalr_token', 'tfc_hostname', 'tfc_token', 'tfc_organization']
    missing_args = [arg for arg in required_args if not getattr(args, arg)]
    if missing_args:
        ConsoleOutput.error(f"Missing required arguments: {', '.join(missing_args)}")
        sys.exit(1)

    if args.migrate_variable_sets_only and args.skip_variable_sets:
        ConsoleOutput.error("--migrate-variable-sets-only cannot be used together with --skip-variable-sets")
        sys.exit(1)

    if args.migrate_variable_sets_only and args.skip_variables == "*":
        ConsoleOutput.error(
            "--migrate-variable-sets-only cannot be used together with --skip-variables=\"*\", "
            "there would be nothing left to migrate"
        )
        sys.exit(1)

    try:
        migrator_args = MigratorArgs.from_argparse(args)
    except InvalidInputError as e:
        ConsoleOutput.error(str(e))
        sys.exit(1)

    try:
        if migrator_args.inventory:
            exit_code = InventoryExporter(migrator_args).run()
        else:
            migration_service = MigrationService(migrator_args)
            if migrator_args.dry_run:
                exit_code = MigrationPlanner(migration_service).run()
            elif migrator_args.verify:
                exit_code = MigrationVerifier(migration_service).run()
            else:
                exit_code = migration_service.run()
    except APIError as e:
        ConsoleOutput.error(f"Unable to migrate workspaces from TFC/E to Scalr. {e}")
        sys.exit(1)
    except Exception as e:
        ConsoleOutput.error(f"Migration failed: {e}")
        if migrator_args.debug_enabled:
            ConsoleOutput.debug(traceback.format_exc())
        sys.exit(1)

    sys.exit(exit_code)
