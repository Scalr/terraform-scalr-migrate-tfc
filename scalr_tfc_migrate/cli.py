"""Command-line entrypoint."""
import argparse
import sys
import traceback

from scalr_tfc_migrate.args import MigratorArgs
from scalr_tfc_migrate.console import ConsoleOutput
from scalr_tfc_migrate.constants import DEFAULT_MANAGEMENT_ENV_NAME
from scalr_tfc_migrate.errors import APIError
from scalr_tfc_migrate.service import MigrationService


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
    parser.add_argument('--pc-name', type=str, help='Provider configuration name')
    parser.add_argument('--agent-pool-name', type=str, help='Scalr agent pool name')
    parser.add_argument('-w', '--workspaces', type=str, help='Workspaces to migrate. By default - all')
    parser.add_argument('--skip-workspace-creation', action='store_true',
                        help='Whether to create new workspaces in Scalr. Set to True if the workspace is already created in Scalr.')
    parser.add_argument('--skip-backend-secrets', action='store_true',
                        help='Whether to create shell variables (`SCALR_` and `TFC_`) in Scalr.')
    parser.add_argument('--skip-tfc-lock', action='store_true', help='Whether to skip locking of TFC/E workspaces')
    parser.add_argument('--management-env-name', type=str, default=DEFAULT_MANAGEMENT_ENV_NAME,
                        help=f'Name of the management environment. Default: {DEFAULT_MANAGEMENT_ENV_NAME}')
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
    parser.add_argument('--credentials-set-name', type=str, help='Skip migration of TFC variable sets to Scalr')

    args = parser.parse_args()

    required_args = ['scalr_hostname', 'scalr_token', 'tfc_hostname', 'tfc_token', 'tfc_organization']
    missing_args = [arg for arg in required_args if not getattr(args, arg)]
    if missing_args:
        ConsoleOutput.error(f"Missing required arguments: {', '.join(missing_args)}")
        sys.exit(1)

    migrator_args = MigratorArgs.from_argparse(args)
    try:
        migration_service = MigrationService(migrator_args)
        migration_service.migrate()
    except APIError as e:
        ConsoleOutput.error(f"Unable to migrate workspaces from TFC/E to Scalr: {e}")
        sys.exit(1)
    except Exception as e:
        ConsoleOutput.error(f"Migration failed: {e}")
        if migrator_args.debug_enabled:
            ConsoleOutput.debug(traceback.format_exc())
        sys.exit(1)
