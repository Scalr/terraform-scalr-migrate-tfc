import binascii
import argparse
import fnmatch
import hashlib
import json
import sys
import traceback
import urllib.error
import urllib.request
from urllib.parse import urlencode
from typing import Dict, List, Optional, Any
import os
import re
from datetime import datetime
from dataclasses import dataclass
import time
from packaging import version

# Check Python version
if sys.version_info < (3, 12):
    sys.exit("Python 3.12 or higher is required")

# Constants
MAX_TERRAFORM_VERSION = "1.5.7"
DEFAULT_MANAGEMENT_ENV_NAME = "terraform-management"
DEFAULT_MANAGEMENT_WORKSPACE_NAME = "workspace-management"
RATE_LIMIT_DELAY = 5  # seconds
MAX_RETRIES = 3

class RateLimitError(Exception):
    pass

class VCSMissingError(Exception):
    pass

def handle_rate_limit(response: urllib.response.addinfourl) -> None:
    """Handle rate limit responses and wait if necessary."""
    if response.status == 429:  # Too Many Requests
        retry_after = int(response.headers.get('Retry-After', RATE_LIMIT_DELAY))
        print(f"Rate limit hit. Waiting {retry_after} seconds...")
        time.sleep(retry_after)
        raise RateLimitError("Rate limit hit")

def make_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    data: Any = None,
    retries: int = MAX_RETRIES
) -> urllib.response.addinfourl:
    """Make HTTP request with rate limit handling and retries."""
    if headers is None:
        headers = {}
    
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, method=method, headers=headers, data=data)
            with urllib.request.urlopen(request) as response:
                handle_rate_limit(response)
                return response
        except RateLimitError:
            if attempt == retries - 1:
                raise
            time.sleep(RATE_LIMIT_DELAY)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                handle_rate_limit(e)
                if attempt == retries - 1:
                    raise
                time.sleep(RATE_LIMIT_DELAY)
            else:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(RATE_LIMIT_DELAY)

@dataclass
class MigratorArgs:
    scalr_hostname: str
    scalr_token: str
    scalr_environment: Optional[str]
    tf_hostname: str
    tf_token: str
    tf_organization: str
    account_id: str
    vcs_name: Optional[str]
    workspaces: str
    skip_workspace_creation: bool
    skip_backend_secrets: bool
    lock: bool
    management_workspace_name: str
    management_env_name: str = DEFAULT_MANAGEMENT_ENV_NAME
    disable_deletion_protection: bool = False
    debug_enabled: bool = False

    @classmethod
    def from_argparse(cls, args: argparse.Namespace) -> 'MigratorArgs':
        return cls(
            scalr_hostname=args.scalr_hostname,
            scalr_token=args.scalr_token,
            scalr_environment=args.scalr_environment,
            tf_hostname=args.tf_hostname,
            tf_token=args.tf_token,
            tf_organization=args.tf_organization,
            account_id=args.account_id,
            vcs_name=args.vcs_name,
            workspaces=args.workspaces or "*",
            skip_workspace_creation=args.skip_workspace_creation,
            skip_backend_secrets=args.skip_backend_secrets,
            lock=args.lock,
            management_env_name=args.management_env_name,
            management_workspace_name=f"{args.scalr_environment}",
            disable_deletion_protection=args.disable_deletion_protection
        )

class HClAttribute:
    def __init__(self, address) -> None:
        self.hcl_value: str = address

class AbstractTerraformResource:
    def __init__(self, resource_type: str, name: str, attributes: Dict, hcl_resource_type: str) -> None:
        self.resource_type = resource_type
        self.name = name
        self.attributes = attributes
        self.id = None
        self.hcl_resource_type: str = hcl_resource_type

    def to_hcl(self) -> str:
        attrs = []
        for key, value in self.attributes.items():
            if key == "vcs_repo" and self.resource_type == "scalr_workspace":
                # Special handling for vcs_repo block in scalr_workspace
                attrs.append("  vcs_repo {")
                for repo_key, repo_value in value.items():
                    if repo_value is not None:  # Skip None values
                        if isinstance(repo_value, str):
                            attrs.append(f'    {repo_key} = "{repo_value}"')
                        elif isinstance(repo_value, bool):
                            attrs.append(f'    {repo_key} = {str(repo_value).lower()}')
                        elif isinstance(repo_value, list):
                            attrs.append(f'    {repo_key} = {json.dumps(repo_value)}')
                attrs.append("  }")
            elif isinstance(value, str):
                attrs.append(f'  {key} = "{value}"')
            elif isinstance(value, bool):
                attrs.append(f'  {key} = {str(value).lower()}')
            elif isinstance(value, dict):
                attrs.append(f'  {key} = {json.dumps(value)}')
            elif isinstance(value, HClAttribute):
                attrs.append(f'  {key} = {value.hcl_value}')
            elif isinstance(value, AbstractTerraformResource):
                attrs.append(f'  {key} = {value.get_address()}')
            else:
                attrs.append(f'  {key} = {value}')
        
        return f'{self.hcl_resource_type} "{self.resource_type}" "{self.name}" {{\n{chr(10).join(attrs)}\n}}'

    def get_address(self):
        return f"{self.hcl_resource_type}.{self.resource_type}.{self.name}.id"

class TerraformResource(AbstractTerraformResource):
    def __init__(self, resource_type: str, name: str, attributes: Dict) -> None:
        super().__init__(resource_type, name, attributes, "resource")

class TerraformDataSource(AbstractTerraformResource):
    def __init__(self, resource_type: str, name: str, attributes: Dict) -> None:
        super().__init__(resource_type, name, attributes, "data")


def extract_resources(attrs_block: str) -> Dict:
    attrs = {}

    # Parse attributes from the block
    for line in attrs_block.split('\n'):
        line = line.strip()
        if '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()
            # Handle string values
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            # Handle boolean values
            elif value.lower() in ('true', 'false'):
                value = value.lower() == 'true'
            # Handle vcs_repo block
            elif key.strip() == 'vcs_repo':
                vcs_attrs = {}
                vcs_block = re.search(r'vcs_repo\s*{([^}]+)}', attrs_block, re.DOTALL)
                if vcs_block:
                    for vcs_line in vcs_block.group(1).split('\n'):
                        vcs_line = vcs_line.strip()
                        if '=' in vcs_line:
                            vcs_key, vcs_value = vcs_line.split('=', 1)
                            vcs_key = vcs_key.strip()
                            vcs_value = vcs_value.strip()
                            if vcs_value.startswith('"') and vcs_value.endswith('"'):
                                vcs_value = vcs_value[1:-1]
                            elif vcs_value.lower() in ('true', 'false'):
                                vcs_value = vcs_value.lower() == 'true'
                            vcs_attrs[vcs_key] = vcs_value
                attrs[key] = vcs_attrs
                continue
            attrs[key] = value
    return attrs

class ResourceManager:
    def __init__(self, output_dir: str) -> None:
        self.resources: List[TerraformResource] = []
        self.data_sources: List[TerraformDataSource] = []
        self.output_dir = output_dir
        self._load_existing_data_sources()
        self._load_existing_resources()

    def _load_existing_resources(self):
        """Load existing resources from main.tf if it exists."""
        main_tf_path = os.path.join(self.output_dir, "main.tf")
        if not os.path.exists(main_tf_path):
            return

        regexp = r'resource\s+"([^"]+)"\s+"([^"]+)"\s*{([^}]+)}'

        with open(main_tf_path, "r") as f:
            for match in re.finditer(regexp, f.read(), re.DOTALL):
                resource_type, name, attrs_block = match.groups()
                self.resources.append(
                    TerraformResource(resource_type, name, extract_resources(attrs_block))
                )

    def _load_existing_data_sources(self):
        """Load existing resources from main.tf if it exists."""
        main_tf_path = os.path.join(self.output_dir, "main.tf")
        if not os.path.exists(main_tf_path):
            return

        regexp = r'data\s+"([^"]+)"\s+"([^"]+)"\s*{([^}]+)}'

        with open(main_tf_path, "r") as f:
            for match in re.finditer(regexp, f.read(), re.DOTALL):
                resource_type, name, attrs_block = match.groups()
                self.data_sources.append(
                    TerraformDataSource(resource_type, name, extract_resources(attrs_block))
                )

    def add_resource(self, resource: TerraformResource):
        """Add a resource if it doesn't already exist."""
        # Check if resource already exists
        if self.has_resource(resource.resource_type, resource.name):
            return

        self.resources.append(resource)

    def add_data_source(self, data_source: TerraformDataSource):
        """Add a resource if it doesn't already exist."""
        # Check if resource already exists
        if self.has_data_source(data_source.resource_type, data_source.name):
            return
        self.data_sources.append(data_source)

    def has_resource(self, resource_type: str, name: str) -> bool:
        for existing in self.resources:
            if existing.resource_type == resource_type and existing.name == name:
                return True

        return  False

    def has_data_source(self, resource_type: str, name: str) -> bool:
        for existing in self.data_sources:
            if existing.resource_type == resource_type and existing.name == name:
                return True

        return  False

    def write_resources(self, output_dir: str):
        os.makedirs(output_dir, exist_ok=True)
        
        # Write main.tf
        main_tf_path = os.path.join(output_dir, "main.tf")
        file_exists = os.path.exists(main_tf_path)
        
        with open(main_tf_path, "a" if file_exists else "w") as f:
            if not file_exists:
                f.write("# Generated by Scalr Migrator\n")
                f.write(f"# Generated at: {datetime.now().isoformat()}\n\n")
                # Add required provider block only for new file
                f.write('''terraform {
  required_providers {
    scalr = {
      source = "scalr/scalr"
    }
  }
}

''')
            
            # Only write new resources
            existing_resources = set()
            if file_exists:
                with open(main_tf_path, "r") as existing:
                    content = existing.read()
                    for resource in (self.resources + self.data_sources):
                        pattern = f'{resource.hcl_resource_type} "{resource.resource_type}" "{resource.name}"'
                        if pattern in content:
                            existing_resources.add((resource.resource_type, resource.name))
            
            # Write resource blocks
            for resource in (self.data_sources + self.resources):
                if (resource.resource_type, resource.name) not in existing_resources:
                    f.write(resource.to_hcl() + "\n\n")

        # Write imports.tf
        imports_path = os.path.join(output_dir, "imports.tf")
        with open(imports_path, "w") as f:
            f.write("# Generated by Scalr Migrator\n")
            f.write(f"# Generated at: {datetime.now().isoformat()}\n\n")
            f.write("# This file contains import blocks for resources.\n")
            f.write("# You can safely remove this file after successful import.\n\n")
            
            for resource in self.resources:
                if resource.id and (resource.resource_type, resource.name) not in existing_resources:
                    f.write(f'import {{\n')
                    f.write(f'  to = {resource.resource_type}.{resource.name}\n')
                    f.write(f'  id = "{resource.id}"\n')
                    f.write(f'}}\n\n')

class APIClient:
    def __init__(self, hostname: str, token: str, api_version: str = "v2"):
        self.hostname = hostname
        self.token = token
        self.api_version = api_version
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.api+json",
        }

    def _encode_filters(self, filters: Optional[Dict] = None) -> str:
        encoded = ''
        if filters:
            encoded = f"?{urlencode(filters)}"
        return encoded

    def make_request(self, url: str, method: str = "GET", data: Dict = None) -> Dict:
        if data:
            data = json.dumps(data).encode('utf-8')
        
        req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
        
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            print(f"\r\nURL: {url}\r\nResponse: {error_body}")
            raise e

    def get(self, route: str, filters: Optional[Dict] = None) -> Dict:
        url = f"https://{self.hostname}/api/{self.api_version}/{route}{self._encode_filters(filters)}"
        return self.make_request(url)

    def post(self, route: str, data: Dict) -> Dict:
        url = f"https://{self.hostname}/api/{self.api_version}/{route}"
        return self.make_request(url, method="POST", data=data)

class TFCClient(APIClient):
    def __init__(self, hostname: str, token: str):
        super().__init__(hostname, token, "v2")

    def get_organization(self, org_name: str) -> Dict:
        return self.get(f"organizations/{org_name}")

    def get_workspaces(self, org_name: str, page: int = 1, page_size: int = 100) -> Dict:
        filters = {
            "page[size]": page_size,
            "page[number]": page,
        }
        return self.get(f"organizations/{org_name}/workspaces", filters)

    def get_workspace_vars(self, org_name: str, workspace_name: str) -> Dict:
        filters = {
            "filter[workspace][name]": workspace_name,
            "filter[organization][name]": org_name,
        }
        return self.get("vars", filters)

    def get_workspace_runs(self, workspace_id: str, page_size: int = 1) -> Dict:
        filters = {"page[size]": page_size}
        return self.get(f"workspaces/{workspace_id}/runs", filters)

    def get_run_plan(self, run_id: str) -> Dict:
        return self.get(f"runs/{run_id}/plan/json-output")

    def lock_workspace(self, workspace_id: str, reason: str) -> Dict:
        return self.post(f"workspaces/{workspace_id}/actions/lock", {"reason": reason})

class ScalrClient(APIClient):
    def __init__(self, hostname: str, token: str):
        super().__init__(hostname, token, "iacp/v3")

    def get_environment(self, name: str) -> Optional[Dict]:
        try:
            response = self.get("environments", filters={"filter[name]": name})
            environments = response.get("data", [])

            return environments[0] if environments else None
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    def get_workspace(self, environment_id, name: str) -> Optional[Dict]:
        try:
            response = self.get("workspaces", {"query": name, "filter[environment]": environment_id})
            workspaces = response.get("data", [])

            return workspaces[0] if workspaces else None
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    def create_environment(self, name: str, account_id: str) -> Dict:
        data = {
            "data": {
                "type": "environments",
                "attributes": {
                    "name": name,
                },
                "relationships": {
                    "account": {
                        "data": {
                            "id": account_id,
                            "type": "accounts"
                        }
                    }
                }
            }
        }
        return self.post("environments", data)

    def create_workspace(self, env_id: str, attributes: Dict, vcs_id: Optional[str] = None) -> Dict:
        data = {
            "data": {
                "type": "workspaces",
                "attributes": attributes,
                "relationships": {
                    "environment": {
                        "data": {
                            "type": "environments",
                            "id": env_id
                        }
                    }
                }
            }
        }

        if vcs_id:
            data["data"]["relationships"]["vcs-provider"] = {
                "data": {
                    "type": "vcs-providers",
                    "id": vcs_id
                }
            }

        return self.post("workspaces", data)

    def create_state_version(self, workspace_id: str, attributes: Dict) -> Dict:
        data = {
            "data": {
                "type": "state-versions",
                "attributes": attributes,
                "relationships": {
                    "workspace": {
                        "data": {
                            "type": "workspaces",
                            "id": workspace_id
                        }
                    }
                }
            }
        }
        return self.post("state-versions", data)

    def create_variable(
            self,
            key: str,
            value: str,
            category: str,
            sensitive: bool,
            description: Optional[str] = None,
            relationships: Optional[Dict] = None,
    ) -> Optional[Dict]:
        data = {
            "data": {
                "type": "vars",
                "attributes": {
                    "key": key,
                    "value": value,
                    "category": category,
                    "sensitive": sensitive,
                    "description": description,
                },
                "relationships": relationships or {}
            }
        }
        response = self.post("vars", data)

        return response

def _enforce_max_version(tf_version: str, workspace_name) -> str:
    if version.parse(tf_version) > version.parse(MAX_TERRAFORM_VERSION):
        print(f"Warning: Workspace {workspace_name} uses Terraform {tf_version}. "
              f"Downgrading to {MAX_TERRAFORM_VERSION}")
        tf_version = MAX_TERRAFORM_VERSION
    return tf_version

def get_workspace_resource_name(name: str) -> str:
    return f"ws_{name.lower().replace('-', '_')}"

def get_workspace_resource_id(name: str) -> str:
    return f"scalr_workspace.{get_workspace_resource_name(name)}.id"

class MigrationService:
    def __init__(self, args: MigratorArgs):
        self.args: MigratorArgs = args
        self.resource_manager: ResourceManager = ResourceManager(f"generated-terraform/{self.args.scalr_environment}")
        self.tfc: TFCClient = TFCClient(args.tf_hostname, args.tf_token)
        self.scalr: ScalrClient = ScalrClient(args.scalr_hostname, args.scalr_token)

    def get_environment_resource_name(self) -> str:
        return f"env_{self.args.scalr_environment.lower().replace('-', '_')}"

    def get_environment_resource_id(self) -> HClAttribute:
        resource_name = self.get_environment_resource_name()
        if self.resource_manager.has_resource("scalr_environment", resource_name):
            return HClAttribute(f"scalr_environment.{resource_name}.id")
        else:
            return HClAttribute(f"data.scalr_environment.{resource_name}.id")

    def create_environment(self, name: str, skip_terraform: bool = False) -> Dict:
        """Get existing workspace or create a new one."""
        # First try to find existing environment
        environment = self.scalr.get_environment(name)
        env_resource_name = self.get_environment_resource_name()

        if environment:
            if not skip_terraform:
                environment_data_source = TerraformDataSource("scalr_environment", env_resource_name,{"name": name})
                if not self.resource_manager.has_resource("scalr_environment", env_resource_name):
                    self.resource_manager.add_data_source(environment_data_source)
            return environment

        response = self.scalr.create_environment(name, self.args.account_id)["data"]

        if not skip_terraform:
            # Create Terraform resource
            env_resource = TerraformResource("scalr_environment", env_resource_name,{"name": name})
            env_resource.id = response["id"]
            self.resource_manager.add_resource(env_resource)
        print(f"Created environment '{name}'")

        return response

    def get_management_workspace_attributes(self):
        return {"attributes": {
            "name": self.args.management_workspace_name,
            "vcs-provider-id": self.args.vcs_name,
            "terraform-version": MAX_TERRAFORM_VERSION,
            "auto-apply": False,
            "operations": True,
            "deletion-protection-enabled": not self.args.disable_deletion_protection,
        }}

    def create_workspace(
        self,
        env_id: str,
        tf_workspace: Dict,
        vcs_id: Optional[str] = None,
        vcs_data: Optional[TerraformDataSource] = None,
        skip_terraform_resource: Optional[bool] = False
    ) -> Dict:
        attributes = tf_workspace["attributes"]
        """Get existing workspace or create a new one."""
        # First try to find existing workspace
        workspace = self.scalr.get_workspace(env_id, attributes['name'])

        if (workspace is not None) ^ self.args.skip_workspace_creation:
            return workspace

        print(f"Creating workspace {attributes['name']}")

        terraform_version = _enforce_max_version(attributes.get("terraform-version", "1.6.0"), attributes["name"])
        execution_mode = "remote" if attributes.get("operations") else "local"

        workspace_attrs = {
            "name": attributes["name"],
            "auto-apply": attributes["auto-apply"],
            "operations": attributes["operations"],
            "terraform-version": terraform_version,
            "deletion_protection_enabled": not self.args.disable_deletion_protection
        }

        if attributes.get("working-directory"):
            workspace_attrs['working-directory'] = attributes["working-directory"]

        vcs_repo = attributes.get("vcs-repo")
        if vcs_repo:
            repo_data = {
                "identifier": attributes["vcs-repo"]["display-identifier"],
                "dry-runs-enabled": attributes["speculative-enabled"],
                "trigger-prefixes": attributes["trigger-prefixes"]
            }
            workspace_attrs["vcs-repo"] = repo_data

            # Add branch only if it's not empty
            if attributes["vcs-repo"]["branch"]:
                workspace_attrs["vcs-repo"]["branch"] = attributes["vcs-repo"]["branch"]

        response = self.scalr.create_workspace(env_id, workspace_attrs, vcs_id if vcs_repo else None)

        if skip_terraform_resource:
            return response["data"]

        if not vcs_data:
            raise VCSMissingError('VCS Provider is required')

        # Create Terraform resource
        workspace_resource = TerraformResource(
            "scalr_workspace",
            get_workspace_resource_name(attributes["name"]),
            {
                "name": attributes["name"],
                "auto_apply": attributes["auto-apply"],
                "execution_mode": execution_mode,
                "terraform_version": terraform_version,
                "vcs_repo": {
                    "identifier": attributes["vcs-repo"]["display-identifier"],
                    "dry_runs_enabled": attributes["speculative-enabled"],
                    "trigger_prefixes": attributes["trigger-prefixes"]
                },
                "working_directory": attributes["working-directory"],
                "environment_id": self.get_environment_resource_id(),
                "vcs_provider_id": vcs_data,
                "deletion_protection_enabled": not self.args.disable_deletion_protection
            }
        )
        # Add branch only if it's not empty
        if attributes["vcs-repo"]["branch"]:
            workspace_resource.attributes["vcs_repo"]["branch"] = attributes["vcs-repo"]["branch"]
            
        workspace_resource.id = response["data"]["id"]
        self.resource_manager.add_resource(workspace_resource)
        
        return response['data']

    def create_state(self, tf_workspace: Dict, workspace_id: str) -> Dict:
        current_state = tf_workspace["relationships"]["current-state-version"]
        if not current_state or not current_state.get("links"):
            raise Exception("State file is missing")

        current_state_url = current_state["links"]["related"]
        state = self.tfc.make_request(f"https://{self.tfc.hostname}/{current_state_url}")["data"]["attributes"]

        if not state["hosted-state-download-url"]:
            raise Exception("State file URL is unavailable")

        raw_state = self.tfc.make_request(state["hosted-state-download-url"])
        raw_state["terraform_version"] = _enforce_max_version(
            raw_state["terraform_version"],
            tf_workspace["attributes"]["name"]
        )

        state_content = json.dumps(raw_state).encode('utf-8')
        encoded_state = binascii.b2a_base64(state_content)

        state_attrs = {
            "serial": raw_state["serial"],
            "md5": hashlib.md5(state_content).hexdigest(),
            "lineage": raw_state["lineage"],
            "state": encoded_state.decode("utf-8")
        }

        return self.scalr.create_state_version(workspace_id, state_attrs)

    def create_backend_config(self) -> None:
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

    def migrate_workspace(self, tf_workspace: Dict, env: Dict, vcs_id: str, vcs_data: TerraformDataSource) -> bool:
        workspace_name = tf_workspace["attributes"]["name"]
        print(f"\nMigrating workspace {workspace_name} into {env['attributes']['name']}...")

        workspace = self.create_workspace(env['id'], tf_workspace, vcs_id=vcs_id, vcs_data=vcs_data)

        print(f"Migrating state into {workspace['attributes']['name']}...")
        self.create_state(tf_workspace, workspace["id"])

        print("Migrating variables...")
        relationships = {
            "workspace": {
                "data": {
                    "type": "workspaces",
                    "id": workspace["id"]
                }
            }
        }

        workspace_resource_id = get_workspace_resource_id(workspace_name)

        for api_var in self.tfc.get_workspace_vars(self.args.tf_organization, workspace_name)["data"]:
            attributes = api_var["attributes"]
            if not attributes["sensitive"]:
                if attributes["category"] == "env":
                    attributes["category"] = "shell"

                response = self.scalr.create_variable(
                    attributes["key"],
                    attributes["value"],
                    attributes["category"],
                    False,
                    attributes["description"],
                    relationships,
                )

                # Create Terraform resource for non-sensitive variables
                var_resource = TerraformResource(
                    "scalr_variable",
                    f"var_{attributes['key'].lower().replace('-', '_')}",
                    {
                        "key": attributes["key"],
                        "value": attributes["value"],
                        "category": attributes["category"],
                        "workspace_id": HClAttribute(workspace_resource_id)
                    },
                )

                if attributes["description"]:
                    var_resource.attributes["description"] = attributes["description"]

                var_resource.id = response["data"]["id"]
                self.resource_manager.add_resource(var_resource)
            else:
                print(f"Skipping creation of sensitive variable {attributes['key']} with value, will process it with another method")

        # Get sensitive variables from plan
        run = self.tfc.get_workspace_runs(tf_workspace["id"])["data"]
        if run:
            plan = self.tfc.get_run_plan(run[0]["id"])
            if "variables" in plan:
                variables = plan["variables"]
                root_module = plan["configuration"]["root_module"]
                configuration_variables = root_module.get("variables", {})

                for var in configuration_variables:
                    if "sensitive" in configuration_variables[var]:
                        print(f"Creating sensitive variable {var} from the plan file")
                        self.scalr.create_variable(
                            var,
                            variables[var]["value"],
                            "terraform",
                            True,
                            None,
                            relationships
                        )

        # Lock workspace if requested
        if self.args.lock and not tf_workspace["attributes"]["locked"]:
            print(f"Locking {workspace_name}...")
            self.tfc.lock_workspace(tf_workspace["id"], "Locked by migrator")

        return True


    def should_migrate_workspace(self, workspace_name: str) -> bool:
        for pattern in self.args.workspaces.split(','):
            if fnmatch.fnmatch(workspace_name, pattern):
                return True
        return False

    def init_backend_secrets(self):
        if self.args.skip_backend_secrets:
            return

        account_relationships = {
            "account": {
                "data": {
                    "type": "accounts",
                    "id": self.args.account_id
                }
            }
        }

        vars_to_create = {
            "SCALR_HOSTNAME": self.args.scalr_hostname,
            "SCALR_TOKEN": self.args.scalr_token,
            "TFE_HOSTNAME": self.args.tf_hostname,
            "TFE_TOKEN": self.args.tf_token,
        }

        print("Initializing backend secrets...")
        for key in vars_to_create:
            vars_filters = {
                "filter[account]": self.args.account_id,
                "filter[key]": key,
                "filter[environment]": None
            }
            if self.scalr.get("vars", vars_filters)["data"]:
                continue
            print(f"Missing shell variable `{key}`. Creating...")
            self.scalr.create_variable(
                key,
                vars_to_create[key],
                "shell",
                True,
                "Created by migrator",
                account_relationships
            )
        print("Initializing backend secrets... Done")

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
            print(f"Adding Scalr credentials to {credentials_file}...")
            credentials["credentials"][self.args.scalr_hostname] = {
                "token": self.args.scalr_token
            }
            with open(credentials_file, 'w') as f:
                f.write(json.dumps(credentials, indent=2))
            print("Credentials added successfully.")
        else:
            print(f"Credentials for {self.args.scalr_hostname} already exist in {credentials_file}")

    def get_vcs_provider_id(self) -> str:
        vcs_provider = self.scalr.get("vcs-providers", {"query": self.args.vcs_name})["data"][0]
        if not vcs_provider:
            raise VCSMissingError(f"VCS provider with name '{self.args.vcs_name}' not found.")

        return vcs_provider["id"]

    def migrate(self):
        self.init_backend_secrets()
        
        # Get organization and create environment
        organization = self.tfc.get_organization(self.args.tf_organization)["data"]
        if not self.args.scalr_environment:
            self.args.scalr_environment = organization["attributes"]["name"]

        # Create management environment and workspace
        management_env = self.create_environment(self.args.management_env_name, True)
        self.create_workspace(
            management_env["id"],
            self.get_management_workspace_attributes(),
            skip_terraform_resource=True
        )

        # Create or get the main environment
        print(f"Migrating organization {self.args.tf_organization}...")
        env = self.create_environment(self.args.scalr_environment)

        # Create backend configuration for the management workspace
        print("Creating backend configuration for management workspace...")
        self.create_backend_config()

        # Migrate workspaces
        next_page = 1
        skipped_workspaces = []

        vcs_id = self.get_vcs_provider_id()

        vcs_data = TerraformDataSource(
            "scalr_vcs_provider",
            self.args.vcs_name,
            {"name": self.args.vcs_name}
        )
        self.resource_manager.add_data_source(vcs_data)

        while True:
            tfc_workspaces = self.tfc.get_workspaces(self.args.tf_organization, next_page)
            next_page = tfc_workspaces["meta"]["pagination"]["next-page"]

            for tf_workspace in tfc_workspaces["data"]:
                workspace_name = tf_workspace["attributes"]["name"]
                if not self.should_migrate_workspace(workspace_name):
                    skipped_workspaces.append(workspace_name)
                    continue

                try:
                    if not tf_workspace["attributes"]["vcs-repo"]:
                        print(f"\nSkipping workspace CLI-driven workspace '{workspace_name}'.")
                        skipped_workspaces.append(workspace_name)
                        continue

                    result = self.migrate_workspace(tf_workspace, env, vcs_id, vcs_data)

                    if not result:
                        skipped_workspaces.append(workspace_name)
                        continue

                    print(f"Successfully migrated workspace {workspace_name}")
                except Exception as e:
                    print(f"Error migrating workspace {workspace_name}: {str(e)}.")
                    if self.args.debug_enabled:
                        print(f" \nTraceback: {traceback.format_exc()}")
                    skipped_workspaces.append(workspace_name)
                    continue

            if not next_page:
                break
        print(f"Skipped {len(skipped_workspaces)} workspace(s): {', '.join(skipped_workspaces)}")
        # Write generated Terraform resources and import commands

        output_dir = self.resource_manager.output_dir

        self.resource_manager.write_resources(output_dir)
        print(f"\nGenerated Terraform resources and import commands in directory: {output_dir}")
        print(f"Migrating organization {self.args.tf_organization} ({self.args.scalr_environment})... Done")
        
        # Check and update Terraform credentials
        self.check_and_update_credentials()
        
        # Print instructions for importing and pushing state
        print("\nNext steps to complete the migration:")
        print("1. Navigate to the generated_terraform directory:")
        print("   cd generated_terraform")
        print("\n2. Initialize Terraform and apply the configuration:")
        print("   terraform init")
        print("   terraform plan  # Review the import operations")
        print("   terraform apply -auto-approve")
        print("\nNote: The configuration includes import blocks that will:")
        print("   - Import all resources to the state")
        print("   - Apply the configuration to match the imported resources")
        print("\nCredentials have been automatically configured in ~/.terraform.d/credentials.tfrc.json")

def validate_vcs_name(args: argparse.Namespace) -> None:
    if not args.skip_workspace_creation and not args.vcs_name:
        print("Error: If --skip-workspace-creation flag is not set, a valid vcs_name must be passed.")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description='Migrate workspaces from TFC/E to Scalr')
    parser.add_argument('--scalr-hostname', type=str, help='Scalr hostname')
    parser.add_argument('--scalr-token', type=str, help='Scalr token')
    parser.add_argument('--scalr-environment', type=str, help='Optional. Scalr environment to create. By default it takes TFC/E organization name.')
    parser.add_argument('--tf-hostname', type=str, help='TFC/E hostname')
    parser.add_argument('--tf-token', type=str, help='TFC/E token')
    parser.add_argument('--tf-organization', type=str, help='TFC/E organization name')
    parser.add_argument('-a', '--account-id', type=str, help='Scalr account')
    parser.add_argument('-v', '--vcs-name', type=str, help='VCS identifier')
    parser.add_argument('-w', '--workspaces', type=str, help='Workspaces to migrate. By default - all')
    parser.add_argument('--skip-workspace-creation', action='store_true', help='Whether to create new workspaces in Scalr. Set to True if the workspace is already created in Scalr.')
    parser.add_argument('--skip-backend-secrets', action='store_true', help='Whether to create shell variables (`SCALR_` and `TFC_`) in Scalr.')
    parser.add_argument('-l', '--lock', action='store_true', help='Whether to lock TFE workspace')
    parser.add_argument('--management-env-name', type=str, default=DEFAULT_MANAGEMENT_ENV_NAME, help=f'Name of the management environment. Default: {DEFAULT_MANAGEMENT_ENV_NAME}')
    parser.add_argument('--management-workspace-name', type=str, default=DEFAULT_MANAGEMENT_WORKSPACE_NAME, help=f'Name of the management workspace. Default: {DEFAULT_MANAGEMENT_WORKSPACE_NAME}')
    parser.add_argument('--disable-deletion-protection', action='store_true', help='Disable deletion protection in workspace resources. Default: enabled')

    args = parser.parse_args()
    
    # Validate required arguments
    required_args = ['scalr_hostname', 'scalr_token', 'tf_hostname', 'tf_token', 'tf_organization', 'account_id']
    missing_args = [arg for arg in required_args if not getattr(args, arg)]
    if missing_args:
        print(f"Error: Missing required arguments: {', '.join(missing_args)}")
        sys.exit(1)
    
    # Validate vcs_name if needed
    validate_vcs_name(args)

    # Convert argparse namespace to MigratorArgs and run migration
    migrator_args = MigratorArgs.from_argparse(args)
    migration_service = MigrationService(migrator_args)
    migration_service.migrate()

if __name__ == "__main__":
    main()
