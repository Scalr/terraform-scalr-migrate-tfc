"""Core TFC → Scalr migration orchestration."""
import binascii
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Set

from packaging import version

from scalr_tfc_migrate.args import MigratorArgs
from scalr_tfc_migrate.clients import ScalrClient, TFCClient
from scalr_tfc_migrate.console import ConsoleOutput
from scalr_tfc_migrate.constants import MAX_TERRAFORM_VERSION
from scalr_tfc_migrate import errors
from scalr_tfc_migrate.errors import InvalidInputError
from scalr_tfc_migrate.hcl import (
    AbstractTerraformResource,
    HClAttribute,
    HCLObject,
    TerraformDataSource,
    TerraformResource,
)
from scalr_tfc_migrate.resource_manager import ResourceManager
from scalr_tfc_migrate.triggers import handle_trigger_patterns


class MigrationService:

    def __init__(self, args: MigratorArgs):
        self.args: MigratorArgs = args
        self.resource_manager: ResourceManager = ResourceManager(f"generated-terraform/{self.args.scalr_environment}")
        self.tfc: TFCClient = TFCClient(args.tfc_hostname, args.tfc_token)
        self.scalr: ScalrClient = ScalrClient(args.scalr_hostname, args.scalr_token)
        self.environment_resource_id: Optional[AbstractTerraformResource] = None
        self.project_id: Optional[str] = None
        self.vcs_id: Optional[str] = None
        self.vcs_data: Optional[TerraformDataSource] = None
        self.provider_config: Optional[Dict] = None
        self.pc_data: Optional[TerraformDataSource] = None
        self.workspaces_map = {}
        self.agent_pool_id: Optional[str] = None
        self.agent_pool_data: Optional[TerraformDataSource] = None
        self.tofu_version: Optional[Dict] = None
        # Cache for agent pool lookups to avoid duplicate API calls
        self.tfc_agent_pool_cache: Dict[str, Optional[str]] = {}  # workspace_id -> agent_pool_name
        self.scalr_agent_pool_cache: Dict[str, Optional[str]] = {}  # agent_pool_name -> agent_pool_id
        self.agent_pool_data_sources: Dict[str, TerraformDataSource] = {}  # agent_pool_name -> data_source
        self.tfc_workspaces_cache: Dict[str, Dict] = {}  # tfc_workspace_id -> workspace payload
        self.workspace_vars_cache: Dict[str, List[Dict]] = {}  # tfc_workspace_id -> vars list

        self.load_account_id()

    def create_workspace_map(self, tfc_workspace_id, workspace: AbstractTerraformResource) -> None:
        self.workspaces_map[tfc_workspace_id] = workspace

    def get_mapped_scalr_workspace_id(self, tfc_workspace_id) -> AbstractTerraformResource:
        if not self.workspaces_map.get(tfc_workspace_id):
            raise errors.MissingMappingError(
                f"The Scalr workspace for the source TFC workspace {tfc_workspace_id} does not exist or was not created within the current runtime."
            )
        return self.workspaces_map[tfc_workspace_id]

    def cache_tfc_workspace(self, tf_workspace: Dict) -> None:
        self.tfc_workspaces_cache[tf_workspace["id"]] = tf_workspace

    def get_tfc_workspace(self, workspace_id: str) -> Dict:
        if workspace_id not in self.tfc_workspaces_cache:
            self.tfc_workspaces_cache[workspace_id] = self.tfc.get_workspace(workspace_id)
        return self.tfc_workspaces_cache[workspace_id]

    def get_workspace_vars_cached(self, tf_workspace: Dict) -> List[Dict]:
        workspace_id = tf_workspace["id"]
        if workspace_id not in self.workspace_vars_cache:
            workspace_name = tf_workspace["attributes"]["name"]
            vars_payload = self.tfc.get_workspace_vars(self.args.tfc_organization, workspace_name)
            self.workspace_vars_cache[workspace_id] = vars_payload.get("data", [])
        return self.workspace_vars_cache[workspace_id]

    def load_account_id(self):
        accounts = self.scalr.get("accounts")["data"]
        if not accounts:
            ConsoleOutput.error("No account is associated with the given Scalr token.")
            sys.exit(1)
        elif len(accounts) > 1:
            ConsoleOutput.error("The token is associated with more than 1 account.")
            sys.exit(1)
        self.args.account_id = accounts[0]["id"]

    def load_tofu(self):
        default_filters = {
            "filter[software-type]": "opentofu",
            "filter[deprecated]": False,
            "filter[status]": "active",
            "page[size]": 1,
            "page[number]": 1
        }
        if self.args.use_opentofu:
            if self.args.opentofu_version:
                try:
                    if version.parse(MAX_TERRAFORM_VERSION) >= version.parse(self.args.opentofu_version):
                        raise errors.InvalidInputError(f"Opentofu version must be '>=1.6.0', '{self.args.opentofu_version}' given")
                    default_filters["filter[version]"] = self.args.opentofu_version
                except version.InvalidVersion as e:
                    raise errors.InvalidInputError(str(e))

            if not self.tofu_version:
                tofu_version = self.scalr.get('software-versions', default_filters)["data"]
                if not tofu_version:
                    raise InvalidInputError(f"Version '{self.args.opentofu_version}' does not exist")
                latest_version = tofu_version[0]["attributes"]["version"]
                ConsoleOutput.info(
                    f"Migration to Opentofu is enabled, workspaces above 1.5.7 will be migrated to {latest_version}"
                )
                self.tofu_version = latest_version

    def get_vcs_data(self) -> Optional[TerraformDataSource]:
        if self.args.vcs_name and not self.vcs_data:
            self.vcs_data = TerraformDataSource(
                "scalr_vcs_provider",
                self.args.vcs_name,
                {"name": self.args.vcs_name}
            )

            self.resource_manager.add_data_source(self.vcs_data)
        elif not self.args.vcs_name:
            raise errors.MissingDataError(f"VCS provider with name is required if you are migrating VCS-driven workspaces.")

        return self.vcs_data

    def get_pc_data(self) -> Optional[TerraformDataSource]:
        if self.args.pc_name and not self.pc_data:
            self.pc_data = TerraformDataSource(
                "scalr_provider_configuration",
                self.args.pc_name,
                {"name": self.args.pc_name}
            )

            self.resource_manager.add_data_source(self.pc_data)

        return self.pc_data

    def get_project_id(self) -> Optional[str]:
        if not self.args.tfc_project:
            return None

        if not self.project_id:
            project = self.tfc.get_project(self.args.tfc_organization, self.args.tfc_project)
            if not project:
                ConsoleOutput.warning(
                    f"Project '{self.args.tfc_project}' not found in organization '{self.args.tfc_organization}'")
                return None
            self.project_id = project["id"]
            ConsoleOutput.info(f"Found project '{self.args.tfc_project}' with ID: '{self.project_id}'")

        return self.project_id

    def get_environment_resource_id(self) -> Optional[AbstractTerraformResource]:
        if not self.environment_resource_id:
            env_resource = self.resource_manager.get_resource("scalr_environment", self.args.scalr_environment)
            self.environment_resource_id = env_resource
        return self.environment_resource_id

    def create_environment(self, name: str, skip_terraform: bool = False) -> Dict:
        """Get existing workspace or create a new one."""
        # First try to find existing environment
        environment = self.scalr.get_environment(name)

        if environment:
            if not skip_terraform:
                existing_resource = self.resource_manager.get_resource('scalr_environment', name)
                if not existing_resource:
                    environment_data_source = TerraformDataSource("scalr_environment", name, {"name": name})
                    self.resource_manager.add_data_source(environment_data_source)
            return environment

        response = self.scalr.create_environment(name, self.args.account_id)["data"]

        if not skip_terraform:
            # Create Terraform resource
            env_resource = TerraformResource("scalr_environment", self.args.scalr_environment, {"name": name})
            env_resource.id = response["id"]
            self.resource_manager.add_resource(env_resource)
        ConsoleOutput.success(f"Created main environment: {name}")

        return response

    def get_management_workspace_attributes(self):
        iac_platform = 'opentofu' if self.args.use_opentofu else 'terraform'
        tf_version = self.tofu_version if self.args.use_opentofu else MAX_TERRAFORM_VERSION

        return {
            "attributes": {
                "name": self.args.management_workspace_name,
                "vcs-provider-id": self.args.vcs_name,
                "terraform-version": tf_version,
                'iac-platform': iac_platform,
                "auto-apply": False,
                "operations": True,
                "deletion-protection-enabled": not self.args.disable_deletion_protection,
            }}

    def get_agent_pool_data(self) -> Optional[TerraformDataSource]:
        """Get agent pool data source if agent pool name is provided."""
        if self.args.agent_pool_name and not self.agent_pool_data and self.get_agent_pool_id():
            self.agent_pool_data = TerraformDataSource(
                "scalr_agent_pool",
                self.args.agent_pool_name,
                {"name": self.args.agent_pool_name}
            )
            self.resource_manager.add_data_source(self.agent_pool_data)
        return self.agent_pool_data

    def get_agent_pool_id(self) -> str:
        """Get agent pool ID from the data source."""
        if self.args.agent_pool_name and not self.agent_pool_id:
            agent_pools = self.scalr.get('agent-pools', {"filter[name]": self.args.agent_pool_name})['data']
            if not len(agent_pools):
                raise errors.MissingDataError(f"Agent pool with name '{self.args.agent_pool_name}' not found.")
            agent_pool_id = agent_pools[0]["id"]
            self.agent_pool_id = agent_pool_id
            agents = self.scalr.get('agents', {"filter[agent-pool]": agent_pool_id})['data']

            if not len(agents):
                raise errors.MissingDataError(
                    f"Agent pool with name '{self.args.agent_pool_name}' does not have active agents.")

        return self.agent_pool_id

    def find_scalr_agent_pool_by_name(self, agent_pool_name: str) -> Optional[str]:
        """Find Scalr agent pool ID by name with caching."""
        # Check cache first
        if agent_pool_name in self.scalr_agent_pool_cache:
            return self.scalr_agent_pool_cache[agent_pool_name]

        try:
            agent_pools = self.scalr.get('agent-pools', {"filter[name]": agent_pool_name})['data']
            if agent_pools:
                agent_pool_id = agent_pools[0]["id"]
                # Check if the agent pool has active agents
                agents = self.scalr.get('agents', {"filter[agent-pool]": agent_pool_id})['data']
                if agents:
                    ConsoleOutput.info(f"Found matching Scalr agent pool: '{agent_pool_name}'")
                    self.scalr_agent_pool_cache[agent_pool_name] = agent_pool_id
                    return agent_pool_id
                else:
                    ConsoleOutput.warning(f"Scalr agent pool '{agent_pool_name}' found but has no active agents")
                    self.scalr_agent_pool_cache[agent_pool_name] = None
            else:
                ConsoleOutput.warning(f"No Scalr agent pool found with name: '{agent_pool_name}'")
                self.scalr_agent_pool_cache[agent_pool_name] = None
        except Exception as e:
            ConsoleOutput.warning(f"Error searching for agent pool '{agent_pool_name}': {e}")
            self.scalr_agent_pool_cache[agent_pool_name] = None

        return None

    def get_tfc_agent_pool_name(self, tf_workspace: Dict) -> Optional[str]:
        """Extract TFC agent pool name from workspace relationships with caching."""
        workspace_id = tf_workspace.get('id')

        # Check cache first
        if workspace_id in self.tfc_agent_pool_cache:
            return self.tfc_agent_pool_cache[workspace_id]

        relationships = tf_workspace.get('relationships', {})
        agent_pool_rel = relationships.get('agent-pool')

        if not agent_pool_rel:
            self.tfc_agent_pool_cache[workspace_id] = None
            return None

        # Get the agent pool ID from the relationship
        agent_pool_data = agent_pool_rel.get('data')
        if not agent_pool_data:
            self.tfc_agent_pool_cache[workspace_id] = None
            return None

        agent_pool_id = agent_pool_data.get('id')
        if not agent_pool_id:
            self.tfc_agent_pool_cache[workspace_id] = None
            return None

        # Fetch the agent pool details to get the name
        try:
            agent_pool_info = self.tfc.get_agent_pool(agent_pool_id)
            if agent_pool_info and agent_pool_info.get('data'):
                agent_pool_name = agent_pool_info['data']['attributes']['name']
                ConsoleOutput.info(f"TFC workspace has agent pool: '{agent_pool_name}'")
                self.tfc_agent_pool_cache[workspace_id] = agent_pool_name
                return agent_pool_name
        except Exception as e:
            ConsoleOutput.warning(f"Error fetching TFC agent pool details: {e}")

        self.tfc_agent_pool_cache[workspace_id] = None
        return None

    def get_or_create_agent_pool_data_source(self, agent_pool_name: str) -> TerraformDataSource:
        """Get or create a cached agent pool data source."""
        if agent_pool_name not in self.agent_pool_data_sources:
            data_source_name = agent_pool_name.replace("-", "_").replace(" ", "_")
            agent_pool_data = TerraformDataSource(
                "scalr_agent_pool",
                data_source_name,
                {"name": agent_pool_name}
            )
            self.resource_manager.add_data_source(agent_pool_data)
            self.agent_pool_data_sources[agent_pool_name] = agent_pool_data

        return self.agent_pool_data_sources[agent_pool_name]

    def enforce_max_version(self, tf_version: str, resource_type: str) -> str:
        range_versions_map = {
            "~>1.4.0": "1.4.7",
            "~>1.5.0": "1.5.7",
        }

        if tf_version in range_versions_map:
            return range_versions_map[tf_version]

        if "~>" in tf_version:
            tf_version = tf_version.strip("~>")

        if tf_version == "latest" or version.parse(tf_version) > version.parse(MAX_TERRAFORM_VERSION):
            if not self.args.use_opentofu:
                ConsoleOutput.warning(f"Workspace uses Terraform {tf_version}. Downgrading to {MAX_TERRAFORM_VERSION}")
                tf_version = MAX_TERRAFORM_VERSION
            elif tf_version != "latest" or resource_type != "Workspace":
                ConsoleOutput.info(
                    f"{resource_type} uses Terraform {tf_version}. Using OpenTofu {self.tofu_version} instead of downgrading."
                )
                tf_version = self.tofu_version

        return tf_version

    def create_workspace(
            self,
            env_id: str,
            tf_workspace: Dict,
            is_management_workspace: Optional[bool] = False
    ) -> AbstractTerraformResource:
        attributes = tf_workspace["attributes"]
        """Get existing workspace or create a new one."""
        # First try to find existing workspace
        workspace = self.scalr.get_workspace(env_id, attributes['name'])

        if workspace is not None:
            workspace_data = TerraformDataSource("scalr_workspace", env_id, workspace['attributes'])
            workspace_data.id = workspace["id"]
            if tf_workspace.get("id"):
                self.create_workspace_map(tf_workspace['id'], workspace_data)
            return workspace_data

        ConsoleOutput.info(f"Creating workspace '{attributes['name']}'...")

        terraform_version = self.enforce_max_version(attributes.get("terraform-version", "1.6.0"), 'Workspace')
        # TFC returns null for these fields when the workspace inherits the value from
        # organization defaults rather than setting it explicitly. Scalr's API rejects
        # null for these required booleans, so fall back to TFC's documented defaults.
        auto_apply = attributes.get("auto-apply")
        if auto_apply is None:
            auto_apply = False
        operations = attributes.get("operations")
        if operations is None:
            operations = True
        speculative_enabled = attributes.get("speculative-enabled")
        if speculative_enabled is None:
            speculative_enabled = True
        execution_mode = "remote" if operations else "local"
        global_remote_state = attributes.get("global-remote-state", False)

        platform = "opentofu" if (
                self.args.use_opentofu and
                (terraform_version == "latest" or version.parse(terraform_version) > version.parse(
                    MAX_TERRAFORM_VERSION))
        ) else "terraform"

        working_directory: str = attributes.get("working-directory")
        working_directory = working_directory.lstrip('/') if working_directory else None

        workspace_attrs = {
            "name": attributes["name"],
            "auto-apply": auto_apply,
            "operations": operations,
            "terraform-version": terraform_version,
            "working-directory": working_directory,
            "deletion-protection-enabled": not self.args.disable_deletion_protection,
            "remote-state-sharing": global_remote_state,
            "iac-platform": platform,
        }

        vcs_id = None
        branch = None
        trigger_patterns = None
        trigger_prefixes = None
        configuration = self.get_provider_configuration()
        pc_id = configuration["id"] if not is_management_workspace and configuration else None
        vcs_repo = attributes.get("vcs-repo")

        if vcs_repo:
            vcs_id = self.get_vcs_provider_id()
            branch = vcs_repo["branch"] if vcs_repo['branch'] else None
            trigger_prefixes: list[str] = attributes.get("trigger-prefixes", [])
            trigger_patterns = attributes.get("trigger-patterns", vcs_repo.get("trigger-patterns", []))

            if not trigger_prefixes:
                trigger_prefixes = vcs_repo.get("trigger-prefixes", [])

            if working_directory and working_directory not in trigger_prefixes and not trigger_patterns:
                trigger_prefixes.append(working_directory)

            trigger_prefixes = [p for p in trigger_prefixes if p is not None]

            workspace_attrs["vcs-repo"] = {
                "identifier": attributes["vcs-repo-identifier"],
                "dry-runs-enabled": speculative_enabled,
                "branch": branch,
                "ingress-submodules": vcs_repo["ingress-submodules"],
            }
            if trigger_prefixes:
                workspace_attrs["vcs-repo"]["trigger-prefixes"] = trigger_prefixes

            if trigger_patterns:
                trigger_patterns = handle_trigger_patterns(attributes["trigger-patterns"])
                workspace_attrs["vcs-repo"]["trigger-patterns"] = trigger_patterns

        relationships = tf_workspace.get('relationships', {})

        # Determine agent pool ID and name to use
        agent_pool_id = None
        agent_pool_name_for_terraform = None

        if relationships.get("agent-pool"):
            # First, try to find matching Scalr agent pool by TFC agent pool name
            tfc_agent_pool_name = self.get_tfc_agent_pool_name(tf_workspace)
            if tfc_agent_pool_name:
                agent_pool_id = self.find_scalr_agent_pool_by_name(tfc_agent_pool_name)
                if agent_pool_id:
                    agent_pool_name_for_terraform = tfc_agent_pool_name

            # If no matching agent pool found and global agent pool is configured, use it as fallback
            if not agent_pool_id and self.args.agent_pool_name:
                ConsoleOutput.info(
                    f"No matching agent pool found, using configured agent pool: '{self.args.agent_pool_name}'")
                agent_pool_id = self.get_agent_pool_id()
                agent_pool_name_for_terraform = self.args.agent_pool_name

        response = self.scalr.create_workspace(env_id, workspace_attrs, vcs_id, agent_pool_id)

        ConsoleOutput.success(f"Created workspace '{attributes['name']}'")

        if pc_id:
            self.scalr.link_provider_config(response["data"]["id"], pc_id)
            ConsoleOutput.info(f"Linked provider configuration: {self.args.pc_name}")

        # Create Terraform resource
        resource_attributes = {
            "name": attributes["name"],
            "auto_apply": auto_apply,
            "execution_mode": execution_mode,
            "terraform_version": terraform_version,
            "working_directory": working_directory,
            "environment_id": self.get_environment_resource_id(),
            "deletion_protection_enabled": not self.args.disable_deletion_protection,
            "iac_platform": platform,
        }

        if global_remote_state:
            resource_attributes["remote_state_consumers"] = HClAttribute(["*"])

        if vcs_repo:
            resource_attributes["vcs_repo"] = {
                "identifier": attributes["vcs-repo-identifier"],
                "dry_runs_enabled": speculative_enabled,
                "branch": branch,
                "ingress_submodules": vcs_repo["ingress-submodules"],
            }
            resource_attributes["vcs_provider_id"] = self.get_vcs_data()
            if trigger_prefixes:
                resource_attributes["vcs_repo"]["trigger_prefixes"] = trigger_prefixes

            if trigger_patterns:
                resource_attributes["vcs_repo"]["trigger_patterns"] = trigger_patterns

        if pc_id:
            resource_attributes["provider_configuration"] = HCLObject({"id": self.get_pc_data()})

        if agent_pool_id and agent_pool_name_for_terraform:
            # Create or get cached data source for the agent pool
            resource_attributes["agent_pool_id"] = self.get_or_create_agent_pool_data_source(
                agent_pool_name_for_terraform)

        workspace_resource = TerraformResource(
            "scalr_workspace",
            attributes["name"],
            resource_attributes
        )

        workspace_resource.id = response["data"]["id"]
        if tf_workspace.get('id'):
            self.create_workspace_map(tf_workspace['id'], workspace_resource)

        if not is_management_workspace:
            self.resource_manager.add_resource(workspace_resource)

        return workspace_resource

    def create_state(self, tf_workspace: Dict, workspace: AbstractTerraformResource) -> None:
        current_scalr_state = self.scalr.get_current_state(workspace.id)
        current_tfc_state = self.tfc.get_current_state(tf_workspace)
        if not current_tfc_state:
            ConsoleOutput.warning("State file is unavailable")
            return

        state = current_tfc_state["attributes"]

        if not state["hosted-state-download-url"]:
            ConsoleOutput.warning("State file URL is unavailable")
            return

        raw_state = self.tfc.make_request(state["hosted-state-download-url"])
        serial = current_scalr_state["data"]["attributes"]["serial"] if current_scalr_state else None
        if serial == raw_state["serial"]:
            ConsoleOutput.info(f"State with serial '{serial}' is up-to-date")
            return

        raw_state["terraform_version"] = self.enforce_max_version(raw_state["terraform_version"], 'State file')

        if workspace.attributes.get('terraform-version'):
            if version.parse(raw_state["terraform_version"]) > version.parse(
                    workspace.attributes.get('terraform-version')):
                ConsoleOutput.warning(
                    'Terraform version of the current state is bigger then workspace version, upgrading workspace')
                self.scalr.update_workspace(workspace.id, {
                    "data": {"attributes": {"terraform_version": raw_state["terraform_version"]}, "type": "workspaces"}
                })

        state_content = json.dumps(raw_state).encode('utf-8')
        encoded_state = binascii.b2a_base64(state_content)

        state_attrs = {
            "serial": raw_state["serial"],
            "md5": hashlib.md5(state_content).hexdigest(),
            "lineage": raw_state["lineage"],
            "state": encoded_state.decode("utf-8")
        }

        self.scalr.create_state_version(workspace.id, state_attrs)

    def create_backend_config(self) -> None:
        if self.args.skip_post_migration:
            return

        ConsoleOutput.info("Creating remote backend configuration...")

        """Create backend configuration for the management workspace."""
        backend_config = f'''terraform {{
  backend "remote" {{
    hostname = "{self.args.scalr_hostname}"
    organization = "{self.args.management_env_name}"
    workspaces {{
      name = "{self.args.management_workspace_name}"
    }}
  }}
}}
'''
        output_dir = self.resource_manager.output_dir
        os.makedirs(output_dir, exist_ok=True)

        with open(os.path.join(output_dir, "backend.tf"), "w") as f:
            f.write("# Generated by Scalr Migrator\n")
            f.write(f"# Generated at: {datetime.now().isoformat()}\n\n")
            f.write(backend_config)

        ConsoleOutput.success("Backend remote configuration created, starting workspaces migration...")

    def migrate_variable(
            self,
            workspace: AbstractTerraformResource,
            var_key: str,
            var_value,
            category: str,
            is_hcl: bool,
            is_sensitive: bool,
            description: str = None
    ) -> None:
        relationships = {
            "workspace": {
                "data": {
                    "type": "workspaces",
                    "id": workspace.id
                }
            }
        }

        try:
            response = self.scalr.create_variable(
                var_key,
                var_value,
                category,
                is_sensitive,
                is_hcl,
                description,
                relationships,
            )
        except errors.APIError as e:
            if e.code == 422:
                ConsoleOutput.info(f"Variable '{var_key}' already exists")
                return
            raise e

        # Create Terraform resource for non-sensitive variables
        var_resource = TerraformResource(
            "scalr_variable",
            f"{workspace.name}_{var_key}",
            {
                "key": var_key,
                "description": description,
                "value": HClAttribute(var_value, True) if is_hcl else var_value,
                "category": category,
                "workspace_id": workspace,
                "hcl": is_hcl,
                "sensitive": is_sensitive,
            },
        )

        var_resource.id = response["data"]["id"]
        self.resource_manager.add_resource(var_resource)

    def migrate_workspace(self, tf_workspace: Dict, env: Dict) -> bool:
        workspace_name = tf_workspace["attributes"]["name"]
        self.cache_tfc_workspace(tf_workspace)
        ConsoleOutput.section(f"Migrating workspace '{workspace_name}' into '{env['attributes']['name']}'...")

        workspace = self.create_workspace(env['id'], tf_workspace)

        ConsoleOutput.info(f"Migrating state...")
        self.create_state(tf_workspace, workspace)

        # Skip variable migration if requested
        if self.args.skip_variables == "*":
            ConsoleOutput.info("Skipping all variable migration as requested")
            return True

        ConsoleOutput.info("Migrating variables...")

        skipped_sensitive_vars = {}
        skipped_shell_vars = []
        skip_patterns = self.args.skip_variables.split(',') if self.args.skip_variables else []

        for api_var in self.get_workspace_vars_cached(tf_workspace):
            attributes = api_var["attributes"]
            is_hcl = attributes["hcl"]
            var_key: str = attributes["key"]

            # Skip variable if it matches any of the skip patterns
            if any(fnmatch.fnmatch(var_key, pattern.strip()) for pattern in skip_patterns):
                ConsoleOutput.info(f"Skipping variable '{var_key}' as requested")
                continue

            if attributes["category"] == "env":
                attributes["category"] = "shell"

            category: str = attributes["category"]

            if attributes["sensitive"]:
                msg = f"Skipping creation of sensitive {category} variable '{var_key}'"
                if category == "terraform" or var_key.startswith('TF_VAR_'):
                    msg += ", will try to create it from the plan file"
                    skipped_sensitive_vars.update({var_key: attributes})
                if category == "shell":
                    msg += ", will try to create if from the TFC environment"
                    skipped_shell_vars.append(var_key)
                ConsoleOutput.info(msg)
                continue

            var_value = attributes["value"]
            self.migrate_variable(workspace, var_key, var_value, category, is_hcl, False, attributes["description"])

        self.migrate_sensitive_terraform_variables(skipped_sensitive_vars, tf_workspace, workspace)
        self.migrate_sensitive_environment_variables(skipped_shell_vars, tf_workspace, workspace)

        if self.args.lock:
            if tf_workspace["attributes"]["locked"]:
                ConsoleOutput.info("Workspace is already locked")
                return True

            env_name = self.args.scalr_environment
            self.tfc.lock_workspace(
                tf_workspace["id"],
                f"Workspace is migrated to the Scalr environment '{env_name}' with name '{workspace_name}'."
            )
            ConsoleOutput.success(f"Workspace '{workspace_name}' is locked")

        return True

    def migrate_sensitive_terraform_variables(self, skipped_sensitive_vars, tf_workspace, workspace):
        # Get sensitive variables from plan
        if not skipped_sensitive_vars:
            return
        plan = self.tfc.get_latest_plan(tf_workspace)
        if not plan:
            ConsoleOutput.warning("Unable to find a plan to migrate sensitive variables")
            return

        if not "variables" in plan:
            ConsoleOutput.warning("Unable to find variables in plan file to migrate sensitive variables")
            return

        ConsoleOutput.info("Plan file is available, reading its variables...")
        variables = plan["variables"]
        root_module = plan["configuration"]["root_module"]
        configuration_variables = root_module.get("variables", {})

        for var in configuration_variables:
            if not "sensitive" in configuration_variables[var]:
                continue

            ConsoleOutput.info(f"Creating sensitive variable '{var}' from the plan file")
            self.migrate_variable(
                workspace,
                var,
                variables[var]["value"],
                "terraform",
                skipped_sensitive_vars[var]["hcl"],
                True,
                skipped_sensitive_vars[var]["description"]
            )

    @staticmethod
    def trigger_sync_job(working_directory: str, args: List[str]):
        operation = f"{args[0]} {args[1]}"  # e.g. "terraform_plan"
        ConsoleOutput.info(f"Starting job `{operation}`...")

        job = subprocess.Popen(
            args,
            cwd=working_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        return_code = job.wait()

        if return_code == 0:
            ConsoleOutput.info("Init succeeded")
        else:
            print("Init failed")
            stdout, stderr = job.communicate()
            print("STDOUT:\n", stdout)
            if stderr:
                print("STDERR:\n", stderr)

    @staticmethod
    def trigger_background_job(working_directory: str, args: List[str]):
        """
        Run a Terraform command fully detached, logging to <command>.log.
        Process survives main process exit.
        """
        operation = f"{args[0]} {args[1]}"  # e.g. "terraform_plan"
        ConsoleOutput.info(
            f"Starting a background job `{operation}` in TFC to finish sensitive environment variables migration...")
        log_file = os.path.join(working_directory, f"{operation}.log")

        # Open log file directly for the child; no shell redirection needed.
        log_fd = open(log_file, "w")

        proc = subprocess.Popen(
            args,
            cwd=working_directory,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )

        # Close in parent, child keeps the fd.
        log_fd.close()

        ConsoleOutput.info(f"{operation} detached → {log_file} to track plan progress (pid={proc.pid})")
        return log_file

    def migrate_sensitive_environment_variables(
            self,
            skipped_shell_vars: List[str],
            tf_workspace: Dict,
            workspace: Optional[AbstractTerraformResource] = None,
            var_set_id: Optional[str] = None,
    ) -> bool:
        if not skipped_shell_vars:
            return False

        if not workspace and not var_set_id:
            ConsoleOutput.warning("Sensitive environment migration target is missing (workspace or var-set)")
            return False

        if self.args.skip_backend_secrets:
            self.tfc.init_backend_secrets(self.args)
            self.args.skip_backend_secrets = True

        ConsoleOutput.info("Migrating sensitive environment variables...")
        working_directory = self.tfc.get_current_cv(tf_workspace)

        if not working_directory:
            ConsoleOutput.info("Cannot migrate sensitive environment variables as configuration version does not exist")
            return False

        backend_config = f'''terraform {{
  cloud {{
    organization = "{self.args.tfc_organization}"
    workspaces {{
      name = "{tf_workspace['attributes']['name']}"
    }}
  }}
}}
'''

        with open(os.path.join(working_directory, "scalr_backend_override.tf"), "w") as f:
            f.write(backend_config)

        templates_dir = "./templates"

        shutil.copy2(os.path.join(templates_dir, "export.tf"), os.path.join(working_directory, "export.tf"))
        shutil.copy2(os.path.join(templates_dir, "migrate-environment.py"),
                     os.path.join(working_directory, "migrate-environment.py"))

        plan_args = [
            'terraform',
            'plan',
            '-input=false',
            f"-var=export_vars_to_scalr={json.dumps(skipped_shell_vars)}",
        ]
        if workspace:
            plan_args.append(f"-var=scalr_workspace_id={workspace.id}")
        if var_set_id:
            plan_args.append(f"-var=scalr_var_set_id={var_set_id}")

        self.trigger_sync_job(working_directory, ['terraform', 'init', '-input=false'])
        self.trigger_background_job(working_directory, plan_args)
        return True

    @staticmethod
    def normalize_variable_category(category: str) -> str:
        return "shell" if category == "env" else category

    @staticmethod
    def _tfc_var_set_relationship_ids(tf_var_set: Dict, relationship_key: str) -> Set[str]:
        """Read resource IDs from JSON:API relationships on a varset (e.g. list/get payload)."""
        rel = (tf_var_set.get("relationships") or {}).get(relationship_key) or {}
        data = rel.get("data")
        if data is None:
            return set()
        if isinstance(data, dict):
            rid = data.get("id")
            return {rid} if rid else set()
        return {item["id"] for item in data if item.get("id")}

    def _tfc_var_set_with_relationships(self, tf_var_set: Dict) -> Dict:
        """For non-global sets, GET varsets/:id (include workspaces,projects); list rows omit IDs and relationship list URLs 404 on HCP."""
        attrs = tf_var_set.get("attributes") or {}
        if attrs.get("global", False):
            return tf_var_set
        var_set_id = tf_var_set["id"]
        name = attrs.get("name", var_set_id)
        try:
            return self.tfc.get_variable_set(
                var_set_id,
                {"include": "workspaces,projects"},
            )
        except errors.APIError as e:
            ConsoleOutput.warning(
                f"Could not load TFC variable set detail for '{name}' ({var_set_id}) to resolve "
                f"workspace/project links: {e}"
            )
            return tf_var_set

    def list_all_variable_sets(self) -> List[Dict]:
        variable_sets: List[Dict] = []
        page = 1
        while True:
            response = self.tfc.list_variable_sets(self.args.tfc_organization, page)
            variable_sets.extend(response.get("data", []))
            next_page = response.get("meta", {}).get("pagination", {}).get("next-page")
            if not next_page:
                break
            page = next_page
        return variable_sets

    def list_all_variable_set_vars(self, varset_id: str) -> List[Dict]:
        page = 1
        variables: List[Dict] = []
        while True:
            response = self.tfc.get_variable_set_vars(varset_id, page)
            variables.extend(response.get("data", []))
            next_page = response.get("meta", {}).get("pagination", {}).get("next-page")
            if not next_page:
                break
            page = next_page
        return variables

    def list_all_variable_set_vars_safe(self, varset_id: str) -> List[Dict]:
        try:
            return self.list_all_variable_set_vars(varset_id)
        except errors.APIError as e:
            ConsoleOutput.warning(
                f"Could not list TFC variable set vars for {varset_id} ({e}); treating as empty."
            )
            return []

    def list_project_workspaces(self, project_id: str) -> List[Dict]:
        page = 1
        project_workspaces: List[Dict] = []
        while True:
            response = self.tfc.get_workspaces(self.args.tfc_organization, page, project_id=project_id)
            project_workspaces.extend(response.get("data", []))
            for tf_workspace in response.get("data", []):
                self.cache_tfc_workspace(tf_workspace)
            next_page = response.get("meta", {}).get("pagination", {}).get("next-page")
            if not next_page:
                break
            page = next_page
        return project_workspaces

    def get_sorted_workspaces(self, workspace_ids: Set[str]) -> List[Dict]:
        workspaces: List[Dict] = []
        for workspace_id in workspace_ids:
            try:
                workspaces.append(self.get_tfc_workspace(workspace_id))
            except errors.APIError as e:
                ConsoleOutput.warning(f"Unable to read workspace '{workspace_id}': {e}")

        return sorted(
            workspaces,
            key=lambda item: item.get("attributes", {}).get("updated-at", ""),
            reverse=True
        )

    def is_workspace_overriding_terraform_variable(self, tf_workspace: Dict, var_key: str) -> bool:
        for ws_var in self.get_workspace_vars_cached(tf_workspace):
            ws_var_attr = ws_var["attributes"]
            ws_var_category = self.normalize_variable_category(ws_var_attr["category"])
            if ws_var_attr["key"] != var_key:
                continue
            if ws_var_category == "terraform" or ws_var_attr["key"].startswith("TF_VAR_"):
                return True
        return False

    def migrate_var_set_variable(
            self,
            var_set_id: str,
            var_key: str,
            var_value: str,
            category: str,
            is_hcl: bool,
            is_sensitive: bool,
            description: Optional[str] = None
    ) -> None:
        try:
            self.scalr.create_var_set_variable(
                var_set_id=var_set_id,
                key=var_key,
                value=var_value,
                category=category,
                sensitive=is_sensitive,
                is_hcl=is_hcl,
                description=description,
            )
        except errors.APIError as e:
            if e.code == 422:
                ConsoleOutput.info(f"Variable '{var_key}' already exists in variable set '{var_set_id}'")
                return
            raise e

    def migrate_sensitive_varset_terraform_variables(
            self,
            skipped_sensitive_vars: Dict[str, Dict],
            candidate_workspaces: List[Dict],
            scalr_var_set_id: str
    ) -> None:
        if not skipped_sensitive_vars:
            return

        pending_vars = set(skipped_sensitive_vars.keys())
        if not candidate_workspaces:
            ConsoleOutput.warning("Unable to find candidate workspaces to read sensitive variable-set Terraform values")
            return

        for tf_workspace in candidate_workspaces:
            if not pending_vars:
                break

            workspace_name = tf_workspace["attributes"]["name"]
            eligible_vars = {
                var_key for var_key in pending_vars
                if not self.is_workspace_overriding_terraform_variable(tf_workspace, var_key)
            }
            if not eligible_vars:
                ConsoleOutput.info(
                    f"Skipping workspace '{workspace_name}' for sensitive variable-set values as all pending keys are overridden")
                continue

            ConsoleOutput.info(f"Trying to read sensitive variable-set values from workspace '{workspace_name}'")
            plan = self.tfc.get_latest_plan(tf_workspace)
            if not plan or "variables" not in plan:
                ConsoleOutput.warning(f"No plan variables available in workspace '{workspace_name}'")
                continue

            root_module = plan.get("configuration", {}).get("root_module", {})
            config_variables = root_module.get("variables", {})
            plan_variables = plan.get("variables", {})

            migrated_from_workspace = 0
            for var_key in list(eligible_vars):
                config = config_variables.get(var_key, {})
                if not config.get("sensitive"):
                    continue
                if var_key not in plan_variables or "value" not in plan_variables[var_key]:
                    continue

                self.migrate_var_set_variable(
                    var_set_id=scalr_var_set_id,
                    var_key=var_key,
                    var_value=plan_variables[var_key]["value"],
                    category="terraform",
                    is_hcl=skipped_sensitive_vars[var_key]["hcl"],
                    is_sensitive=True,
                    description=skipped_sensitive_vars[var_key]["description"],
                )
                pending_vars.remove(var_key)
                migrated_from_workspace += 1

            if migrated_from_workspace > 0:
                ConsoleOutput.info(
                    f"Migrated {migrated_from_workspace} sensitive Terraform variable-set variable(s) from workspace '{workspace_name}'")

        if pending_vars:
            ConsoleOutput.warning(
                f"Unable to migrate sensitive Terraform variable-set variables: {', '.join(sorted(pending_vars))}"
            )

    def migrate_sensitive_varset_environment_variables(
            self,
            skipped_shell_vars: List[str],
            candidate_workspaces: List[Dict],
            scalr_var_set_id: str
    ) -> None:
        if not skipped_shell_vars:
            return

        if not candidate_workspaces:
            ConsoleOutput.warning(
                "Unable to find candidate workspaces to migrate sensitive variable-set shell variables")
            return

        for tf_workspace in candidate_workspaces:
            workspace_name = tf_workspace["attributes"]["name"]
            ConsoleOutput.info(
                f"Trying to migrate sensitive variable-set shell variables using workspace '{workspace_name}'")
            if self.migrate_sensitive_environment_variables(
                    skipped_shell_vars=skipped_shell_vars,
                    tf_workspace=tf_workspace,
                    var_set_id=scalr_var_set_id
            ):
                return

        ConsoleOutput.warning(
            f"Unable to schedule migration for sensitive variable-set shell variables: {', '.join(skipped_shell_vars)}"
        )

    def should_include_tfc_variable_set(
            self,
            tf_var_set: Dict,
            tfc_project_id: Optional[str],
            migrated_tfc_workspace_ids: Set[str],
    ) -> bool:
        """
        Only migrate variable sets that apply to this run:
        - TFC-global sets, or
        - sets linked to the filtered TFC project (when --tfc-project is set), or
        - sets linked to at least one TFC workspace migrated in this run.
        For non-global sets, callers should pass a varset document from GET varsets/:id (with include)
        so workspace/project linkage is present; relationship list sub-routes are not used.
        """
        if tf_var_set["attributes"].get("global", False):
            return True

        workspace_ids = self._tfc_var_set_relationship_ids(tf_var_set, "workspaces")
        project_ids = self._tfc_var_set_relationship_ids(tf_var_set, "projects")

        if tfc_project_id and tfc_project_id in project_ids:
            return True
        if workspace_ids & migrated_tfc_workspace_ids:
            return True
        return False

    def _scalr_var_set_environment_ids_for_upsert(
            self,
            existing_scalr_var_set_id: Optional[str],
            env_id: str,
            is_global: bool,
    ) -> List[str]:
        """
        Shared (TFC-global) variable sets must not attach explicit environment relationships.
        Non-global sets grant the current Scalr environment and merge with existing when updating by name.
        """
        if is_global:
            return []
        merged: Set[str] = {env_id}
        if existing_scalr_var_set_id:
            try:
                doc = self.scalr.get_var_set(
                    existing_scalr_var_set_id,
                    {"include": "environments"},
                ) or {}
                data = doc.get("data") or {}
                env_rel = (data.get("relationships") or {}).get("environments") or {}
                rel_raw = env_rel.get("data")
                if rel_raw is None:
                    rel: List[Dict] = []
                elif isinstance(rel_raw, list):
                    rel = rel_raw
                else:
                    rel = [rel_raw]
                for item in rel:
                    wid = item.get("id")
                    if wid:
                        merged.add(wid)
                for inc in doc.get("included") or []:
                    if inc.get("type") == "environments" and inc.get("id"):
                        merged.add(inc["id"])
            except errors.APIError as e:
                ConsoleOutput.warning(
                    f"Could not read existing Scalr variable set environments ({e}); using current environment only"
                )
        return sorted(merged)

    def migrate_variable_sets(self, env: Dict, tfc_project_id: Optional[str]) -> None:
        if self.args.skip_variable_sets:
            ConsoleOutput.info("Skipping variable sets migration as requested")
            return

        if self.args.skip_variables == "*":
            ConsoleOutput.info("Skipping variable sets migration as all variable migration is disabled")
            return

        migrated_tfc_workspace_ids: Set[str] = set(self.workspaces_map.keys())
        if not migrated_tfc_workspace_ids and not tfc_project_id:
            ConsoleOutput.info(
                "Skipping variable sets migration: no workspaces were migrated in this run "
                "and no --tfc-project was specified (cannot scope non-global sets to workspaces)"
            )
            return

        variable_sets = self.list_all_variable_sets()
        if not variable_sets:
            ConsoleOutput.info("No variable sets found in source organization")
            return

        ConsoleOutput.section("Migrating variable sets")
        skip_patterns = self.args.skip_variables.split(',') if self.args.skip_variables else []
        skipped_filter = 0

        for tf_var_set in variable_sets:
            var_set_name = tf_var_set["attributes"]["name"]
            if var_set_name == self.args.credentials_set_name:
                ConsoleOutput.info(
                    f"Skipping variable set '{var_set_name}' "
                    "(migrator-created TFC credentials for remote plans; not migrated to Scalr)"
                )
                continue
            tf_resolved = self._tfc_var_set_with_relationships(tf_var_set)
            if not self.should_include_tfc_variable_set(
                    tf_resolved, tfc_project_id, migrated_tfc_workspace_ids
            ):
                skipped_filter += 1
                ConsoleOutput.info(
                    f"Skipping variable set '{var_set_name}' (not global, not linked to this project/workspaces in scope)"
                )
                continue

            ConsoleOutput.info(f"Migrating variable set '{var_set_name}'...")
            try:
                self.migrate_variable_set(
                    tf_resolved, env, skip_patterns, migrated_tfc_workspace_ids
                )
            except Exception as e:
                ConsoleOutput.error(f"Failed to migrate variable set '{var_set_name}': {e}")
                if self.args.debug_enabled:
                    ConsoleOutput.debug(f"Traceback: {traceback.format_exc()}")

        if skipped_filter:
            ConsoleOutput.info(
                f"Skipped {skipped_filter} variable set(s) outside this run's scope "
                f"(global / --tfc-project / migrated workspaces only)"
            )

    def migrate_variable_set(
            self,
            tf_var_set: Dict,
            env: Dict,
            skip_patterns: List[str],
            migrated_tfc_workspace_ids: Set[str],
    ) -> bool:
        var_set_id = tf_var_set["id"]
        tf_var_set_attr = tf_var_set["attributes"]

        is_global = tf_var_set_attr.get("global", False)
        var_set_name = tf_var_set_attr["name"]
        description = tf_var_set_attr.get("description")
        # TFC org-global sets map to Scalr account-wide variable sets (`is-shared`).
        # Non-global sets stay environment-scoped; environment IDs are still merged per run below.
        is_shared_scalr = is_global

        existing_scalr_var_set = self.scalr.get_var_sets(name=var_set_name)["data"]
        existing_id = existing_scalr_var_set[0]["id"] if existing_scalr_var_set else None
        environment_ids = self._scalr_var_set_environment_ids_for_upsert(existing_id, env["id"], is_global)

        if existing_scalr_var_set:
            scalr_var_set = self.scalr.update_var_set(
                var_set_id=existing_scalr_var_set[0]["id"],
                name=var_set_name,
                description=description,
                is_shared=is_shared_scalr,
                environment_ids=environment_ids,
            )["data"]
        else:
            scalr_var_set = self.scalr.create_var_set(
                name=var_set_name,
                description=description,
                is_shared=is_shared_scalr,
                environment_ids=environment_ids,
            )["data"]

        related_workspace_ids: Set[str] = set()
        if is_global:
            related_workspace_ids.update(migrated_tfc_workspace_ids)
        else:
            related_workspace_ids.update(self._tfc_var_set_relationship_ids(tf_var_set, "workspaces"))
            for project_id in self._tfc_var_set_relationship_ids(tf_var_set, "projects"):
                for tf_workspace in self.list_project_workspaces(project_id):
                    related_workspace_ids.add(tf_workspace["id"])

        related_workspace_ids &= migrated_tfc_workspace_ids

        candidate_workspaces = self.get_sorted_workspaces(related_workspace_ids)
        var_set_variables = self.list_all_variable_set_vars_safe(var_set_id)

        skipped_sensitive_tf_vars: Dict[str, Dict] = {}
        skipped_sensitive_shell_vars: List[str] = []
        for tf_var in var_set_variables:
            attributes = tf_var["attributes"]
            var_key: str = attributes["key"]

            if any(fnmatch.fnmatch(var_key, pattern.strip()) for pattern in skip_patterns):
                ConsoleOutput.info(f"Skipping variable-set variable '{var_key}' as requested")
                continue

            category = self.normalize_variable_category(attributes["category"])

            if attributes["sensitive"]:
                msg = f"Skipping creation of sensitive variable-set {category} variable '{var_key}'"
                if category == "terraform" or var_key.startswith('TF_VAR_'):
                    msg += ", will try to create it from a plan file"
                    skipped_sensitive_tf_vars[var_key] = attributes
                elif category == "shell":
                    msg += ", will try to create it from the TFC environment"
                    skipped_sensitive_shell_vars.append(var_key)
                ConsoleOutput.info(msg)
                continue

            self.migrate_var_set_variable(
                var_set_id=scalr_var_set["id"],
                var_key=var_key,
                var_value=attributes["value"],
                category=category,
                is_hcl=attributes["hcl"],
                is_sensitive=False,
                description=attributes["description"],
            )

        self.migrate_sensitive_varset_terraform_variables(
            skipped_sensitive_vars=skipped_sensitive_tf_vars,
            candidate_workspaces=candidate_workspaces,
            scalr_var_set_id=scalr_var_set["id"],
        )
        self.migrate_sensitive_varset_environment_variables(
            skipped_shell_vars=skipped_sensitive_shell_vars,
            candidate_workspaces=candidate_workspaces,
            scalr_var_set_id=scalr_var_set["id"],
        )

        if not is_global:
            linked_workspaces = 0
            skipped_unmapped = 0
            for tf_workspace_id in sorted(related_workspace_ids):
                try:
                    scalr_workspace = self.get_mapped_scalr_workspace_id(tf_workspace_id)
                    self.scalr.add_workspace_variable_sets(scalr_workspace.id, [scalr_var_set["id"]])
                    linked_workspaces += 1
                except errors.MissingMappingError:
                    skipped_unmapped += 1
                except errors.APIError as e:
                    if e.code != 422:
                        raise e

            if linked_workspaces:
                ConsoleOutput.success(
                    f"Successfully migrated variable set: {var_set_name} and linked to {linked_workspaces} workspace(s)"
                )

            if skipped_unmapped:
                ConsoleOutput.warning(
                    f"Skipped linking for {skipped_unmapped} workspace(s) that were not migrated"
                )

    def should_migrate_workspace(self, workspace_name: str) -> bool:
        for pattern in self.args.workspaces.split(','):
            # Clean the pattern by removing quotes and whitespace
            cleaned_pattern = pattern.replace("'", '').replace('"', '').strip()

            # Skip empty patterns
            if not cleaned_pattern:
                continue

            try:
                # Use fnmatch-style pattern matching for simpler, safer pattern matching
                # Convert shell-style wildcards to regex
                if '*' in cleaned_pattern or '?' in cleaned_pattern:
                    # Convert shell wildcards to regex
                    regex_pattern = cleaned_pattern.replace('*', '.*').replace('?', '.')
                    # Escape other regex special characters except . and *
                    regex_pattern = re.escape(regex_pattern).replace(r'\.\*', '.*').replace(r'\.', '.')
                else:
                    # For patterns without wildcards, use exact match (case-insensitive)
                    regex_pattern = re.escape(cleaned_pattern)

                if re.search(regex_pattern, workspace_name, re.IGNORECASE):
                    return True
            except re.error as e:
                # If regex fails, fall back to simple string matching
                ConsoleOutput.warning(
                    f"Warning: Invalid pattern '{cleaned_pattern}': {e}. Using simple string matching.")
                if cleaned_pattern.lower() in workspace_name.lower():
                    return True
        return False

    def init_backend_secrets(self):
        if self.args.skip_backend_secrets:
            return
        self.scalr.init_backend_secrets(self.args)
        self.tfc.init_backend_secrets(self.args)

        ConsoleOutput.success("Initialized backend secrets")

    def check_and_update_credentials(self) -> None:
        """Check and update Terraform credentials for Scalr hostname."""
        credentials_file = os.path.expanduser("~/.terraform.d/credentials.tfrc.json")
        os.makedirs(os.path.dirname(credentials_file), exist_ok=True)

        # Read existing credentials or create new structure
        if os.path.exists(credentials_file):
            try:
                with open(credentials_file, 'r') as f:
                    credentials = json.load(f)
            except json.JSONDecodeError:
                credentials = {"credentials": {}}
        else:
            credentials = {"credentials": {}}

        # Check if credentials for Scalr hostname exist
        if self.args.scalr_hostname not in credentials["credentials"]:
            ConsoleOutput.info(f"Adding Scalr credentials to {credentials_file}...")
            credentials["credentials"][self.args.scalr_hostname] = {
                "token": self.args.scalr_token
            }
            with open(credentials_file, 'w') as f:
                f.write(json.dumps(credentials, indent=2))
            ConsoleOutput.success("Credentials added successfully.")
        else:
            ConsoleOutput.info(f"Credentials for {self.args.scalr_hostname} already exist in {credentials_file}")

    def get_vcs_provider_id(self) -> str:
        if self.args.vcs_name and not self.vcs_id:
            vcs_provider = self.scalr.get("vcs-providers", {"query": self.args.vcs_name})["data"]
            if not vcs_provider:
                raise errors.MissingDataError(f"VCS provider with name '{self.args.vcs_name}' not found.")
            self.vcs_id = vcs_provider[0]["id"]

        return self.vcs_id

    def get_provider_configuration(self) -> Dict:
        if self.args.pc_name and not self.provider_config:
            pc_provider = self.scalr.get("provider-configurations", {"filter[name]": self.args.pc_name})["data"][0]
            if not pc_provider:
                raise errors.MissingDataError(f"Provider configuration with name '{self.args.pc_name}' not found.")
            self.provider_config = pc_provider

        return self.provider_config

    def update_provider_configuration(self, env_id: str) -> None:
        provider_configuration = self.get_provider_configuration()
        if not provider_configuration:
            return

        if provider_configuration["attributes"]["is-shared"]:
            return

        allowed_environments = provider_configuration["relationships"].get('environments')
        data = allowed_environments.get('data', [])

        for allowed_environment in data:
            if allowed_environment['id'] == env_id:
                return

        data.append({
            "id": env_id,
            'type': 'environments',
        })

        attributes = {
            'data': {
                'type': 'provider-configurations',
                'id': provider_configuration["id"],
                'relationships': {
                    'environments': {
                        'data': data
                    }
                }
            }
        }

        self.scalr.patch(f"provider-configurations/{provider_configuration['id']}", attributes)

    def migrate(self):
        ConsoleOutput.section("Preparing migration")
        self.init_backend_secrets()
        self.load_tofu()

        # Get organization and create environment
        tf_organization = self.args.tfc_organization
        self.tfc.get_organization(tf_organization)

        project_msg = ''
        if self.args.tfc_project:
            project_msg = f" (project '{self.args.tfc_project}')"

        # Get project ID if specified
        project_id = self.get_project_id()
        if project_id:
            ConsoleOutput.info(f"Filtering TFC workspaces by project: '{self.args.tfc_project}'")

        ConsoleOutput.info(
            f"Migrating organization '{tf_organization}'{project_msg} into environment '{self.args.scalr_environment}'"
        )

        if not self.args.skip_post_migration:
            # Create management environment and workspace
            ConsoleOutput.info(f"Creating post-management Scalr environment '{self.args.management_env_name}'...")
            management_env = self.create_environment(self.args.management_env_name, skip_terraform=True)

            ConsoleOutput.info(f"Creating post-management Scalr workspace '{self.args.management_workspace_name}'...")
            self.create_workspace(
                management_env["id"],
                self.get_management_workspace_attributes(),
                is_management_workspace=True
            )

        # Create or get the main environment
        ConsoleOutput.info(f"Creating destination Scalr environment '{self.args.scalr_environment}'...")
        env = self.create_environment(self.args.scalr_environment)
        self.update_provider_configuration(env['id'])

        # Create backend configuration for the management workspace
        self.create_backend_config()

        # Migrate workspaces
        next_page = 1
        skipped_workspaces = []
        successful_workspaces = []
        workspace_state_consumers = {}

        while True:
            tfc_workspaces = self.tfc.get_workspaces(tf_organization, next_page, project_id=project_id)
            next_page = tfc_workspaces["meta"]["pagination"]["next-page"]

            for tf_workspace in tfc_workspaces["data"]:
                self.cache_tfc_workspace(tf_workspace)
                workspace_name = tf_workspace["attributes"]["name"]
                state_consumers = tf_workspace["relationships"].get('remote-state-consumers')
                if not tf_workspace["attributes"].get("global-remote-state", False) and state_consumers:
                    tfc_consumers = self.tfc.get_by_short_url(state_consumers['links']['related'])['data']
                    if tfc_consumers:
                        workspace_state_consumers.update({tf_workspace['id']: {
                            "consumers":tfc_consumers,
                            "workspace_name": workspace_name,
                        }})

                if not self.should_migrate_workspace(workspace_name):
                    skipped_workspaces.append(workspace_name)
                    continue

                try:
                    result = self.migrate_workspace(tf_workspace, env)

                    if not result:
                        skipped_workspaces.append(workspace_name)
                        continue

                    successful_workspaces.append(workspace_name)
                    ConsoleOutput.success(f"Successfully migrated workspace: {workspace_name}")
                except Exception as e:
                    ConsoleOutput.error(f"Error migrating workspace {workspace_name}: {str(e)}")
                    if self.args.debug_enabled:
                        ConsoleOutput.debug(f"Traceback: {traceback.format_exc()}")
                    skipped_workspaces.append(workspace_name)
                    continue

            if not next_page:
                break

        if len(workspace_state_consumers):
            ConsoleOutput.section("Post-migrating state consumers")
        for tfc_id, consumers_data in workspace_state_consumers.items():
            try:
                scalr_id = self.get_mapped_scalr_workspace_id(tfc_id).id
                consumer_ids = []
                consumer_resources: List[AbstractTerraformResource] = []
                for state_consumer in consumers_data['consumers']:
                    consumer = self.get_mapped_scalr_workspace_id(state_consumer['id'])
                    consumer_ids.append(consumer.id)
                    consumer_resources.append(consumer)

                if consumer_ids:
                    self.scalr.update_consumers(scalr_id, consumer_ids)

                    self.resource_manager.get_resource(
                        'scalr_workspace',
                        consumers_data['workspace_name']
                    ).add_attribute('remote_state_consumers', consumer_resources)
                    ConsoleOutput.info(f"Updated state consumers for workspace '{scalr_id}'...")
            except errors.MissingMappingError as e:
                ConsoleOutput.warning(f"Unable to post-migrate state consumers. {e}")
                continue
            except RuntimeError as e:
                ConsoleOutput.warning(e.args[0])
                continue
            except errors.APIError as e:
                ConsoleOutput.error(f"Unable to update remote state consumers: {e}")
                continue

        self.migrate_variable_sets(env, project_id)

        ConsoleOutput.section("Migration Summary")
        ConsoleOutput.success(f"Successfully migrated {len(successful_workspaces)} workspace(s)")
        if skipped_workspaces:
            ConsoleOutput.warning(f"Skipped {len(skipped_workspaces)} workspace(s): {', '.join(skipped_workspaces)}")

        # Write generated Terraform resources
        output_dir = self.resource_manager.output_dir
        self.resource_manager.write_resources(output_dir)
        ConsoleOutput.success(f"Generated Terraform configuration in directory: {output_dir}")

        # Check and update Terraform credentials
        self.check_and_update_credentials()
        ConsoleOutput.info("Credentials have been automatically configured in ~/.terraform.d/credentials.tfrc.json")

