"""CLI / runtime arguments."""
import argparse
import os
from dataclasses import dataclass
from typing import Optional

from scalr_tfc_migrate.constants import DEFAULT_MANAGEMENT_ENV_NAME


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
    skip_workspace_creation: bool
    skip_backend_secrets: bool
    management_workspace_name: str
    agent_pool_name: Optional[str] = None
    account_id: Optional[str] = None
    lock: bool = True
    tfc_project: Optional[str] = None
    management_env_name: str = DEFAULT_MANAGEMENT_ENV_NAME
    disable_deletion_protection: bool = False
    debug_enabled: bool = os.getenv("SCALR_DEBUG_ENABLED", False)
    skip_variables: Optional[str] = None
    use_opentofu: bool = False
    skip_post_migration: bool = False

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
            skip_workspace_creation=args.skip_workspace_creation,
            skip_backend_secrets=args.skip_backend_secrets,
            lock=not args.skip_tfc_lock,
            management_env_name=args.management_env_name,
            management_workspace_name=args.scalr_environment.replace(" ", '-'),
            disable_deletion_protection=args.disable_deletion_protection,
            skip_variables=args.skip_variables,
            use_opentofu=args.use_opentofu,
            skip_post_migration=args.skip_post_migration,
        )

