"""CLI / runtime arguments."""
import argparse
import os
from dataclasses import dataclass
from typing import Optional, Set

from scalr_tfc_migrate import constants
from scalr_tfc_migrate.matching import load_workspace_names


@dataclass
class MigratorArgs:
    scalr_hostname: str
    scalr_token: str
    tfc_hostname: str
    tfc_token: str
    tfc_organization: str
    scalr_environment: str
    vcs_name: Optional[str]
    pc_name: Optional[str]
    workspaces: str
    skip_backend_secrets: bool
    management_workspace_name: str
    credentials_set_name: str
    agent_pool_name: Optional[str] = None
    pc_map_file: Optional[str] = None
    skip_provider_credentials: bool = False
    account_id: Optional[str] = None
    lock: bool = True
    tfc_project: Optional[str] = None
    management_env_name: str = constants.DEFAULT_MANAGEMENT_ENV_NAME
    disable_deletion_protection: bool = False
    debug_enabled: bool = os.getenv("SCALR_DEBUG_ENABLED", False)
    skip_variables: Optional[str] = None
    use_opentofu: bool = False
    opentofu_version: Optional[str] = None
    skip_post_migration: bool = False
    skip_variable_sets: bool = False
    migrate_variable_sets_only: bool = False
    workspaces_file: Optional[str] = None
    # Lower-cased workspace names read from --workspaces-file; None when no file is given.
    workspace_names: Optional[Set[str]] = None
    report_file: Optional[str] = None
    inventory: bool = False
    inventory_dir: str = constants.DEFAULT_INVENTORY_DIR
    inventory_format: str = constants.INVENTORY_FORMAT_CSV
    dry_run: bool = False
    team_map_file: Optional[str] = None
    team_access_file: Optional[str] = None
    workspace_defaults_file: Optional[str] = None
    skip_run_triggers: bool = False
    layout: str = constants.LAYOUT_SINGLE
    verify: bool = False
    verify_plan: bool = False

    @classmethod
    def from_argparse(cls, args: argparse.Namespace) -> 'MigratorArgs':
        if not args.scalr_environment:
            args.scalr_environment = args.tfc_project if args.tfc_project else args.tfc_organization

        # Defaults to the destination environment name, keeping the historical behavior
        # when --management-workspace-name is not given.
        management_workspace_name = args.management_workspace_name or args.scalr_environment

        return cls(
            scalr_hostname=args.scalr_hostname,
            scalr_token=args.scalr_token,
            scalr_environment=args.scalr_environment,
            tfc_hostname=args.tfc_hostname,
            tfc_token=args.tfc_token,
            tfc_organization=args.tfc_organization,
            tfc_project=args.tfc_project,
            vcs_name=args.vcs_name,
            pc_name=args.pc_name,
            pc_map_file=args.pc_map,
            skip_provider_credentials=args.skip_provider_credentials,
            agent_pool_name=args.agent_pool_name,
            workspaces=args.workspaces or "*",
            skip_backend_secrets=args.skip_backend_secrets,
            lock=not args.skip_tfc_lock,
            management_env_name=args.management_env_name,
            management_workspace_name=management_workspace_name.replace(" ", '-'),
            disable_deletion_protection=args.disable_deletion_protection,
            skip_variables=args.skip_variables,
            use_opentofu=args.use_opentofu,
            opentofu_version=args.opentofu_version,
            skip_post_migration=args.skip_post_migration,
            skip_variable_sets=args.skip_variable_sets,
            migrate_variable_sets_only=args.migrate_variable_sets_only,
            workspaces_file=args.workspaces_file,
            workspace_names=load_workspace_names(args.workspaces_file) if args.workspaces_file else None,
            report_file=args.report_file,
            inventory=args.inventory,
            inventory_dir=args.inventory_dir or constants.DEFAULT_INVENTORY_DIR,
            inventory_format=args.inventory_format or constants.INVENTORY_FORMAT_CSV,
            dry_run=args.dry_run,
            team_map_file=args.team_map,
            team_access_file=args.team_access,
            workspace_defaults_file=args.workspace_defaults,
            skip_run_triggers=args.skip_run_triggers,
            layout=args.layout or constants.LAYOUT_SINGLE,
            verify=args.verify or args.verify_plan,
            verify_plan=args.verify_plan,
            credentials_set_name=args.credentials_set_name if args.credentials_set_name else constants.TFC_MIGRATOR_DEFAULT_SECRETS_VARSET_NAME,
        )
