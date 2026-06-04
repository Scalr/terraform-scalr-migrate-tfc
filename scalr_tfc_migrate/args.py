"""CLI / runtime arguments."""
import argparse
import os
from dataclasses import dataclass
from typing import Optional

from scalr_tfc_migrate import constants


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

    @classmethod
    def from_argparse(cls, args: argparse.Namespace) -> 'MigratorArgs':
        if not args.scalr_environment:
            args.scalr_environment = args.tfc_project if args.tfc_project else args.tfc_organization

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
            agent_pool_name=args.agent_pool_name,
            workspaces=args.workspaces or "*",
            skip_backend_secrets=args.skip_backend_secrets,
            lock=not args.skip_tfc_lock,
            management_env_name=args.management_env_name,
            management_workspace_name=args.scalr_environment.replace(" ", '-'),
            disable_deletion_protection=args.disable_deletion_protection,
            skip_variables=args.skip_variables,
            use_opentofu=args.use_opentofu,
            opentofu_version=args.opentofu_version,
            skip_post_migration=args.skip_post_migration,
            skip_variable_sets=args.skip_variable_sets,
            credentials_set_name=args.credentials_set_name if args.credentials_set_name else constants.TFC_MIGRATOR_DEFAULT_SECRETS_VARSET_NAME,
        )
