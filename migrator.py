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
DEFAULT_MANAGEMENT_ENV_NAME = "scalr-admin"
RATE_LIMIT_DELAY = 5  # seconds
MAX_RETRIES = 3

class RateLimitError(Exception):
    pass

class MissingDataError(Exception):
    pass

def handle_rate_limit(response: urllib.response.addinfourl) -> None:
    """Handle rate limit responses and wait if necessary."""
    if response.status == 429:  # Too Many Requests
        retry_after = int(response.headers.get('Retry-After', RATE_LIMIT_DELAY))
        ConsoleOutput.warning(f"Rate limit hit. Waiting {retry_after} seconds...")
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
    debug_enabled: bool = False
    skip_variables: Optional[str] = None

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
            management_workspace_name=f"{args.scalr_environment}",
            disable_deletion_protection=args.disable_deletion_protection,
            skip_variables=args.skip_variables
        )

class HClAttribute:
    def __init__(self, value, encode_required: bool = False) -> None:
        self.hcl_value = value
        self.encode_required = encode_required

    def get_hcl_value(self) -> Any:
        if not self.encode_required:
            return self.hcl_value

        try:
            json.loads(self.hcl_value)
            return json.dumps(self.hcl_value)
        except (ValueError, TypeError):
            return self.hcl_value

class HCLObject:
    def __init__(self, attributes: dict) -> None:
        self.attributes = attributes


class AbstractTerraformResource:
    def __init__(self, resource_type: str, name: str, attributes: Dict, hcl_resource_type: str) -> None:
        self.resource_type = resource_type
        self.name = name.lower().replace('-', '_')
        self.attributes = attributes
        self.id = None
        self.hcl_resource_type: str = hcl_resource_type

    def _render_attribute(self, attrs: list, key, value, ident: Optional[int] = None):
        if not ident:
            ident = 2

        if key == "vcs_repo" and self.resource_type == "scalr_workspace":
            # Special handling for vcs_repo block in scalr_workspace
            attrs.append((" "*ident) + "vcs_repo {")
            for repo_key, repo_value in value.items():
                if repo_value is not None:  # Skip None values
                    if isinstance(repo_value, str):
                        # Special handling for trigger_patterns
                        if repo_key == "trigger_patterns" and '\n' in repo_value:
                            attrs.append((" " * (ident + 2)) + f'{repo_key} = <<EOT')
                            attrs.extend(f'{line}' for line in repo_value.split('\n'))
                            attrs.append('    EOT')
                        else:
                            attrs.append((" " * (ident + 2)) + f'{repo_key} = "{repo_value}"')
                    elif isinstance(repo_value, bool):
                        attrs.append((" " * (ident + 2)) + f'{repo_key} = {str(repo_value).lower()}')
                    elif isinstance(repo_value, list):
                        attrs.append((" " * (ident + 2)) + f'{repo_key} = {json.dumps(repo_value)}')
            attrs.append("  }")
        elif isinstance(value, str):
            # Check if the value contains newlines and use EOT format if it does
            if '\n' in value:
                # Split the value into lines and indent each line
                lines = value.split('\n')
                attrs.append((" " * ident) + f'{key} = <<EOT')
                attrs.extend((" " * ident) + f'{line}' for line in lines)
                attrs.append((" " * ident) + f'EOT')
            else:
                attrs.append((" " * ident) + f'{key} = "{value}"')
        elif isinstance(value, bool):
            attrs.append((" " * ident) + f'{key} = {str(value).lower()}')
        elif isinstance(value, dict):
            attrs.append((" " * ident) + f'{key} = {json.dumps(value)}')
        elif isinstance(value, list):
            attrs.append((" " * ident) + f'{key} = [')
            for v in value:
                if isinstance(v, str):
                    attrs.append(f'"{v}",')
                elif isinstance(v, AbstractTerraformResource):
                    attrs.append((" " * (ident + 2)) + f'{v.get_address()},')
            attrs.append((" " * ident) + ']')

        elif isinstance(value, HClAttribute):
            attrs.append((" " * ident) + f'{key} = {value.get_hcl_value()}')
        elif isinstance(value, AbstractTerraformResource):
            attrs.append((" " * ident) + f'{key} = {value.get_address()}')
        elif isinstance(value, HCLObject):
            attrs.append((" " * ident) + f'{key} ' + '{')
            for hcl_key, hcl_value in value.attributes.items():
                self._render_attribute(attrs, hcl_key, hcl_value, ident + 2)
            attrs.append((" " * ident) + '}')
        elif value is None:
            pass
        else:
            attrs.append((" " * ident) + f'{key} = {value}')

    def to_hcl(self) -> str:
        attrs = []
        for key, value in self.attributes.items():
            self._render_attribute(attrs, key, value)
        
        return f'{self.hcl_resource_type} "{self.resource_type}" "{self.name}" {{\n{chr(10).join(attrs)}\n}}'

    def get_address(self):
        hcl_resource_type = f"{self.hcl_resource_type}." if self.hcl_resource_type == "data" else ''
        return f"{hcl_resource_type}{self.resource_type}.{self.name}.id"

    def add_attribute(self, name: str, value):
        self.attributes[name] = value

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

    def get_resource(self, resource_type: str, name: str) -> Optional[AbstractTerraformResource]:
        converted = name.lower().replace('-', '_')
        for existing in self.data_sources + self.resources:
            if existing.resource_type == resource_type and existing.name == converted:
                return existing
        return None

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
                    rs: str = resource.to_hcl()
                    f.write(rs.replace("'", '"') + "\n\n")

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

    def get_by_short_url(self, short_url: str) -> Dict:
        return self.make_request(f"https://{self.hostname}/{short_url}")

    def make_request(self, url: str, method: str = "GET", data: Dict = None) -> Dict:
        if data:
            data = json.dumps(data).encode('utf-8')
        
        req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
        
        with urllib.request.urlopen(req) as response:
            if response.code != 204:
                return json.loads(response.read().decode('utf-8'))
            return {}

    def get(self, route: str, filters: Optional[Dict] = None) -> Dict:
        url = f"https://{self.hostname}/api/{self.api_version}/{route}{self._encode_filters(filters)}"
        return self.make_request(url)

    def post(self, route: str, data: Dict) -> Dict:
        url = f"https://{self.hostname}/api/{self.api_version}/{route}"
        return self.make_request(url, method="POST", data=data)

    def patch(self, route: str, data: Dict) -> Dict:
        url = f"https://{self.hostname}/api/{self.api_version}/{route}"
        return self.make_request(url, method="PATCH", data=data)

class TFCClient(APIClient):
    def __init__(self, hostname: str, token: str):
        super().__init__(hostname, token, "v2")

    def get_organization(self, org_name: str) -> Dict:
        return self.get(f"organizations/{org_name}")

    def get_workspaces(self, org_name: str, page: int = 1, project_id: Optional[str] = None) -> Dict:
        filters = {
            "page[size]": 100,
            "page[number]": page,
        }
        if project_id:
            filters["filter[project][id]"] = project_id
        return self.get(f"organizations/{org_name}/workspaces", filters)

    def get_project(self, org_name: str, project_name: str) -> Optional[Dict]:
        try:
            response = self.get(f"organizations/{org_name}/projects", {"filter[names]": project_name})
            projects = response.get("data", [])
            return projects[0] if projects else None
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            return None

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
        try:
            return self.get(f"runs/{run_id}/plan/json-output")
        except Exception:
            ConsoleOutput.info("Skipping: plan file is unavailable")
            return {}

    def lock_workspace(self, workspace_id: str, reason: str) -> Dict:
        return self.post(f"workspaces/{workspace_id}/actions/lock", {"reason": reason})

class ScalrClient(APIClient):
    def __init__(self, hostname: str, token: str):
        super().__init__(hostname, token, "iacp/v3")

    def update_consumers(self, workspace_id, consumers: list[str]):
        relationships = []

        for consumer in consumers:
            relationships.append({
                "type": "workspaces",
                "id": consumer
            })

        self.patch(f"workspaces/{workspace_id}/relationships/remote-state-consumers", {"data": relationships})

    def update_workspace(self, workspace_id, payload: Dict):
        return self.patch(f"workspaces/{workspace_id}", payload)

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

    def create_workspace(
        self,
        env_id: str,
        attributes: Dict,
        vcs_id: Optional[str] = None,
        agent_pool_id: Optional[str] = None
    ) -> Dict:
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
                    },
                    "vcs-provider": {"data": {"type": "vcs-providers", "id": vcs_id}} if vcs_id else None,
                    "agent-pool": {"data": {"type": "agent-pools", "id": agent_pool_id}} if agent_pool_id else None,
                }
            }
        }

        return self.post("workspaces", data)

    def link_provider_config(self, workspace_id: str, pc_id: str) -> Dict:
        data = {
            "data": {
                "type": "provider-configuration-links",
                "relationships": {
                    "provider-configuration": {
                        "data": {"id": pc_id, "type": "provider-configurations"}
                    }
                }
            }
        }

        return self.post(f"workspaces/{workspace_id}/provider-configuration-links", data)

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
            is_hcl: bool = False,
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
                    "hcl": is_hcl,
                },
                "relationships": relationships or {}
            }
        }
        response = self.post("vars", data)

        return response

def _enforce_max_version(tf_version: str, workspace_name) -> str:
    if version.parse(tf_version) > version.parse(MAX_TERRAFORM_VERSION):
        ConsoleOutput.warning(f"Warning: {workspace_name} uses Terraform {tf_version}. "
              f"Downgrading to {MAX_TERRAFORM_VERSION}")
        tf_version = MAX_TERRAFORM_VERSION
    return tf_version

class ConsoleOutput:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

    @classmethod
    def info(cls, message: str) -> None:
        print(f"{cls.CYAN}[INFO]{cls.ENDC} {message}")

    @classmethod
    def success(cls, message: str) -> None:
        print(f"{cls.GREEN}[SUCCESS]{cls.ENDC} {message}")

    @classmethod
    def warning(cls, message: str) -> None:
        print(f"{cls.WARNING}[WARNING]{cls.ENDC} {message}")

    @classmethod
    def error(cls, message: str) -> None:
        print(f"{cls.FAIL}[ERROR]{cls.ENDC} {message}")

    @classmethod
    def debug(cls, message: str) -> None:
        print(f"{cls.BLUE}[DEBUG]{cls.ENDC} {message}")

    @classmethod
    def section(cls, message: str) -> None:
        print(f"\n{cls.HEADER}{cls.BOLD}{message}{cls.ENDC}")
        print(f"{cls.HEADER}{'=' * len(message)}{cls.ENDC}\n")

def validate_trigger_pattern(pattern: str) -> bool:
    """
    Validate a trigger pattern format.
    Returns True if the pattern is valid, False otherwise.
    """
    # Skip validation for comments
    if pattern.startswith('#'):
        return True
        
    # Basic validation rules:
    # 1. Pattern should not be empty after stripping
    # 2. Pattern should not contain invalid characters
    # 3. Pattern should follow gitignore-like syntax
    
    pattern = pattern.strip()
    if not pattern:
        return False
        
    # Check for invalid characters (if any)
    # Note: Scalr uses gitignore-like syntax, so most characters are valid
    invalid_chars = ['\n', '\r']  # Newlines are not allowed in patterns
    if any(char in pattern for char in invalid_chars):
        return False
        
    return True

def handle_trigger_patterns(patterns: List[str]) -> Optional[str]:
    """
    Process and validate trigger patterns.
    Returns a multiline string of valid patterns or None if no valid patterns exist.
    """
    try:
        if not patterns:
            return None
        
        validated_patterns = []
        for pattern in patterns:
            if validate_trigger_pattern(pattern):
                validated_patterns.append(pattern)
            else:
                ConsoleOutput.warning(f"Invalid trigger pattern: {pattern}")
        
        return "\n".join(validated_patterns)
    except Exception as e:
        ConsoleOutput.error(f"Error processing trigger patterns: {str(e)}")
        return None


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

        self.load_account_id()

    def create_workspace_map(self, tfc_workspace_id, workspace: AbstractTerraformResource) -> None:
        self.workspaces_map[tfc_workspace_id] = workspace

    def get_mapped_scalr_workspace_id(self, tfc_workspace_id) -> AbstractTerraformResource:
        if not self.workspaces_map.get(tfc_workspace_id):
            raise RuntimeError(f"Workspace {tfc_workspace_id} not found.")
        return self.workspaces_map[tfc_workspace_id]

    def load_account_id(self):
        accounts = self.scalr.get("accounts")["data"]
        if not accounts:
            ConsoleOutput.error("No account is associated with the given Scalr token.")
            sys.exit(1)
        elif len(accounts) > 1:
            ConsoleOutput.error("The token is associated with more than 1 account.")
            sys.exit(1)
        self.args.account_id = accounts[0]["id"]

    def get_vcs_data(self) -> Optional[TerraformDataSource]:
        if self.args.vcs_name and not self.vcs_data:
            self.vcs_data = TerraformDataSource(
                "scalr_vcs_provider",
                self.args.vcs_name,
                {"name": self.args.vcs_name}
            )

            self.resource_manager.add_data_source(self.vcs_data)

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
                ConsoleOutput.warning(f"Project '{self.args.tfc_project}' not found in organization '{self.args.tfc_organization}'")
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
                environment_data_source = TerraformDataSource("scalr_environment", name, {"name": name})
                self.resource_manager.add_data_source(environment_data_source)
            return environment

        response = self.scalr.create_environment(name, self.args.account_id)["data"]

        if not skip_terraform:
            # Create Terraform resource
            env_resource = TerraformResource("scalr_environment", self.args.scalr_environment,{"name": name})
            env_resource.id = response["id"]
            self.resource_manager.add_resource(env_resource)
        ConsoleOutput.success(f"Created main environment: {name}")

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
                raise MissingDataError(f"Agent pool with name '{self.args.agent_pool_name}' not found.")
            agent_pool_id = agent_pools[0]["id"]
            self.agent_pool_id = agent_pool_id
            agents = self.scalr.get('agents', {"filter[agent-pool]": agent_pool_id})['data']

            if not len(agents):
                raise MissingDataError(f"Agent pool with name '{self.args.agent_pool_name}' does not have active agents.")

        return self.agent_pool_id

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

        if (workspace is not None) ^ self.args.skip_workspace_creation:
            workspace_data = TerraformDataSource("scalr_workspace", env_id, {"name": attributes['name']})
            workspace_data.id = workspace["id"]
            if tf_workspace.get("id"):
                self.create_workspace_map(tf_workspace['id'], workspace_data)
            return workspace_data

        ConsoleOutput.info(f"Creating workspace '{attributes['name']}'...")

        terraform_version = _enforce_max_version(attributes.get("terraform-version", "1.6.0"), attributes["name"])
        execution_mode = "remote" if attributes.get("operations") else "local"
        global_remote_state = attributes.get("global-remote-state", False)

        workspace_attrs = {
            "name": attributes["name"],
            "auto-apply": attributes["auto-apply"],
            "operations": attributes["operations"],
            "terraform-version": terraform_version,
            "working-directory": attributes.get("working-directory"),
            "deletion-protection-enabled": not self.args.disable_deletion_protection,
            "remote-state-sharing": global_remote_state,
        }

        vcs_id = None
        branch = None
        trigger_patterns = None
        pc_id = self.get_provider_configuration()["id"] if not is_management_workspace else None
        vcs_repo = attributes.get("vcs-repo")

        if vcs_repo:
            vcs_id = self.get_vcs_provider_id()
            branch = vcs_repo["branch"] if vcs_repo.get("branch") in vcs_repo else None

            workspace_attrs["vcs-repo"] = {
                "identifier": vcs_repo["display-identifier"],
                "dry-runs-enabled": attributes["speculative-enabled"],
                "trigger-prefixes": attributes["trigger-prefixes"],
                "branch": branch,
                "ingress-submodules": vcs_repo["ingress-submodules"],
            }

            if attributes.get("trigger-prefixes"):
                workspace_attrs["vcs-repo"]["trigger-prefixes"] = attributes["trigger-prefixes"]
            
            if attributes.get("trigger-patterns"):
                trigger_patterns = handle_trigger_patterns(attributes["trigger-patterns"])
                workspace_attrs["vcs-repo"]["trigger-patterns"] = trigger_patterns

        relationships = tf_workspace.get('relationships', {})
        agent_pool_id = self.get_agent_pool_id() if relationships.get("agent-pool") else None
        response = self.scalr.create_workspace(env_id, workspace_attrs, vcs_id, agent_pool_id)

        ConsoleOutput.success(f"Created workspace '{attributes['name']}'")

        if pc_id:
            self.scalr.link_provider_config(response["data"]["id"], pc_id)
            ConsoleOutput.info(f"Linked provider configuration: {self.args.pc_name}")

        # Create Terraform resource
        resource_attributes = {
            "name": attributes["name"],
            "auto_apply": attributes["auto-apply"],
            "execution_mode": execution_mode,
            "terraform_version": terraform_version,
            "working_directory": attributes.get("working-directory"),
            "environment_id": self.get_environment_resource_id(),
            "deletion_protection_enabled": not self.args.disable_deletion_protection
        }

        if global_remote_state:
            resource_attributes["remote_state_consumers"] = HClAttribute(["*"])

        if vcs_repo:
            resource_attributes["vcs_repo"] = {
                "identifier": vcs_repo["display-identifier"],
                "dry_runs_enabled": attributes["speculative-enabled"],
                "branch": branch,
                "ingress_submodules": vcs_repo["ingress-submodules"],
            }
            resource_attributes["vcs_provider_id"] = self.get_vcs_data()
            if attributes.get("trigger-prefixes"):
                resource_attributes["vcs_repo"]["trigger_prefixes"] = attributes["trigger-prefixes"]

            if trigger_patterns:
                resource_attributes["vcs_repo"]["trigger_patterns"] = trigger_patterns

        if pc_id:
            resource_attributes["provider_configuration"] = HCLObject({"id": self.get_pc_data()})

        if agent_pool_id:
            resource_attributes["agent_pool_id"] = self.get_agent_pool_data()

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

    def get_current_state(self, workspace_id: str) -> Optional[Dict]:
        try:
            return self.scalr.get(f"workspaces/{workspace_id}/current-state-version")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    def create_state(self, tf_workspace: Dict, workspace_id: str) -> None:
        current_scalr_state = self.get_current_state(workspace_id)
        current_tf_state = tf_workspace["relationships"]["current-state-version"]

        if not current_tf_state or not current_tf_state.get("links"):
            ConsoleOutput.warning("State file is missing")
            return

        current_state_url = current_tf_state["links"]["related"]
        state = self.tfc.get_by_short_url(current_state_url)["data"]["attributes"]

        if not state["hosted-state-download-url"]:
            ConsoleOutput.warning("State file URL is unavailable")
            return

        raw_state = self.tfc.make_request(state["hosted-state-download-url"])
        serial = current_scalr_state["data"]["attributes"]["serial"] if current_scalr_state else None
        if serial == raw_state["serial"]:
            ConsoleOutput.info(f"State with '{serial}' is up-to-date")
            return

        raw_state["terraform_version"] = _enforce_max_version(raw_state["terraform_version"],'State file')

        state_content = json.dumps(raw_state).encode('utf-8')
        encoded_state = binascii.b2a_base64(state_content)

        state_attrs = {
            "serial": raw_state["serial"],
            "md5": hashlib.md5(state_content).hexdigest(),
            "lineage": raw_state["lineage"],
            "state": encoded_state.decode("utf-8")
        }

        self.scalr.create_state_version(workspace_id, state_attrs)

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

    def migrate_workspace(self, tf_workspace: Dict, env: Dict) -> bool:
        workspace_name = tf_workspace["attributes"]["name"]
        ConsoleOutput.section(f"Migrating workspace '{workspace_name}' into '{env['attributes']['name']}'...")

        workspace = self.create_workspace(env['id'], tf_workspace)

        ConsoleOutput.info(f"Migrating state...")
        self.create_state(tf_workspace, workspace.id)

        # Skip variable migration if requested
        if self.args.skip_variables == "*":
            ConsoleOutput.info("Skipping all variable migration as requested")
            return True

        ConsoleOutput.info("Migrating variables...")
        relationships = {
            "workspace": {
                "data": {
                    "type": "workspaces",
                    "id": workspace.id
                }
            }
        }

        skipped_sensitive_vars = {}
        skip_patterns = self.args.skip_variables.split(',') if self.args.skip_variables else []

        for api_var in self.tfc.get_workspace_vars(self.args.tfc_organization, workspace_name)["data"]:
            attributes = api_var["attributes"]
            var_key: str = attributes["key"]

            # Skip variable if it matches any of the skip patterns
            if any(fnmatch.fnmatch(var_key, pattern.strip()) for pattern in skip_patterns):
                ConsoleOutput.info(f"Skipping variable '{var_key}' as requested")
                continue

            if attributes["category"] == "env":
                attributes["category"] = "shell"

            if attributes["sensitive"]:
                msg = f"Skipping creation of sensitive {attributes['category']} variable '{attributes['key']}'"
                if attributes["category"] == "terraform" or var_key.startswith('TF_VAR_'):
                    msg += ", will try to create it from the plan file"
                    skipped_sensitive_vars.update({attributes["key"]: attributes})
                ConsoleOutput.warning(msg)
                continue

            try:
                response = self.scalr.create_variable(
                    attributes["key"],
                    attributes["value"],
                    attributes["category"],
                    False,
                    attributes["hcl"],
                    attributes["description"],
                    relationships,
                )
            except urllib.error.HTTPError as e:
                if e.code == 422:
                    ConsoleOutput.info(f"Variable '{attributes['key']}' already exists")
                    continue
                raise e

            # Create Terraform resource for non-sensitive variables
            var_resource = TerraformResource(
                "scalr_variable",
                attributes['key'],
                {
                    "key": attributes["key"],
                    "description": attributes["description"],
                    "value": HClAttribute(attributes["value"], True) if attributes["hcl"] else attributes["value"],
                    "category": attributes["category"],
                    "workspace_id": workspace,
                    "hcl": attributes["hcl"],
                },
            )
            var_resource.id = response["data"]["id"]
            self.resource_manager.add_resource(var_resource)

        # Get sensitive variables from plan
        run = self.tfc.get_workspace_runs(tf_workspace["id"])["data"]
        if run:
            ConsoleOutput.info("Trying to migrate sensitive variables...")
            plan = self.tfc.get_run_plan(run[0]["id"])
            if "variables" in plan:
                ConsoleOutput.info("Plan file is available, reading its variables...")
                variables = plan["variables"]
                root_module = plan["configuration"]["root_module"]
                configuration_variables = root_module.get("variables", {})

                for var in configuration_variables:
                    if "sensitive" in configuration_variables[var]:
                        ConsoleOutput.info(f"Creating sensitive variable '{var}' from the plan file")
                        response = self.scalr.create_variable(
                            var,
                            variables[var]["value"],
                            "terraform",
                            True,
                            skipped_sensitive_vars[var]["hcl"],
                            skipped_sensitive_vars[var]["description"],
                            relationships
                        )

                        var_resource = TerraformResource(
                            "scalr_variable",
                            var,
                            {
                                "key":var,
                                "description": skipped_sensitive_vars[var]["description"],
                                "value": HClAttribute(variables[var]["value"], True) if skipped_sensitive_vars[var]["hcl"] else variables[var]["value"],
                                "category": 'terraform',
                                "workspace_id": workspace,
                                "hcl": skipped_sensitive_vars[var]["hcl"],
                            },
                        )

                        var_resource.id = response["data"]["id"]
                        self.resource_manager.add_resource(var_resource)

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
            "TFE_HOSTNAME": self.args.tfc_hostname,
            "TFE_TOKEN": self.args.tfc_token,
        }

        for key in vars_to_create:
            vars_filters = {
                "filter[account]": self.args.account_id,
                "filter[key]": key,
                "filter[environment]": None
            }
            if self.scalr.get("vars", vars_filters)["data"]:
                continue
            try:
                self.scalr.create_variable(
                    key,
                    vars_to_create[key],
                    "shell",
                    True,
                    False,
                    "Created by migrator",
                    account_relationships
                )
            except urllib.error.HTTPError as e:
                if e.code == 422:
                    ConsoleOutput.info(f"Variable '{key}' already exists")
                    continue

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
            vcs_provider = self.scalr.get("vcs-providers", {"query": self.args.vcs_name})["data"][0]
            if not vcs_provider:
                raise MissingDataError(f"VCS provider with name '{self.args.vcs_name}' not found.")
            self.vcs_id = vcs_provider["id"]

        return self.vcs_id

    def get_provider_configuration(self) -> Dict:
        if self.args.pc_name and not self.provider_config:
            pc_provider = self.scalr.get("provider-configurations", {"filter[name]": self.args.pc_name})["data"][0]
            if not pc_provider:
                raise MissingDataError(f"Provider configuration with name '{self.args.pc_name}' not found.")
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
        ConsoleOutput.info("Creating remote backend configuration...")
        self.create_backend_config()
        ConsoleOutput.success("Backend remote configuration created, starting workspaces migration...")

        # Migrate workspaces
        next_page = 1
        skipped_workspaces = []
        successful_workspaces = []
        workspace_state_consumers = {}

        while True:
            tfc_workspaces = self.tfc.get_workspaces(tf_organization, next_page, project_id=project_id)
            next_page = tfc_workspaces["meta"]["pagination"]["next-page"]

            for tf_workspace in tfc_workspaces["data"]:
                workspace_name = tf_workspace["attributes"]["name"]
                state_consumers = tf_workspace["relationships"].get('remote-state-consumers')
                if not tf_workspace["attributes"].get("global-remote-state", False) and state_consumers:
                    workspace_state_consumers.update({tf_workspace['id']: {
                        "url": state_consumers['links']['related'],
                        "workspace_name": workspace_name,
                    }})
                if not self.should_migrate_workspace(workspace_name):
                    skipped_workspaces.append(workspace_name)
                    continue

                try:
                    result = self.migrate_workspace(tf_workspace,env)

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

        ConsoleOutput.section("Post-migrating state consumers")
        for tfc_id, consumers_data in workspace_state_consumers.items():
            try:
                scalr_id = self.get_mapped_scalr_workspace_id(tfc_id).id
                consumer_ids = []
                consumer_resources: List[AbstractTerraformResource] = []
                tfc_consumers = self.tfc.get_by_short_url(consumers_data['url'])['data']
                for state_consumer in tfc_consumers:
                    consumer = self.get_mapped_scalr_workspace_id(state_consumer['id'])
                    consumer_ids.append(consumer.id)
                    consumer_resources.append(consumer)

                if consumer_ids:
                    self.scalr.update_consumers(scalr_id, consumer_ids)

                    self.resource_manager.get_resource(
                        'scalr_workspace',
                        consumers_data['workspace_name']
                    ).add_attribute('remote_state_consumers', consumer_resources)
            except RuntimeError as e:
                ConsoleOutput.warning(e.args[0])
                continue
            except urllib.error.HTTPError as e:
                ConsoleOutput.error(f"Unable to update remote state consumers: {e}")
                continue

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

def validate_vcs_name(args: argparse.Namespace) -> None:
    if not args.skip_workspace_creation and not args.vcs_name:
        ConsoleOutput.error("Error: If --skip-workspace-creation flag is not set, a valid vcs_name must be passed.")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description='Migrate workspaces from TFC/E to Scalr')
    parser.add_argument('--scalr-hostname', type=str, help='Scalr hostname')
    parser.add_argument('--scalr-token', type=str, help='Scalr token')
    parser.add_argument('--scalr-environment', type=str, help='Optional. Scalr environment to create. By default it takes TFC/E organization name.')
    parser.add_argument('--tfc-hostname', type=str, help='TFC/E hostname')
    parser.add_argument('--tfc-token', type=str, help='TFC/E token')
    parser.add_argument('--tfc-organization', type=str, help='TFC/E organization name')
    parser.add_argument('-v', '--vcs-name', type=str, help='VCS identifier')
    parser.add_argument('--pc-name', type=str, help='Provider configuration name')
    parser.add_argument('--agent-pool-name', type=str, help='Scalr agent pool name')
    parser.add_argument('-w', '--workspaces', type=str, help='Workspaces to migrate. By default - all')
    parser.add_argument('--skip-workspace-creation', action='store_true', help='Whether to create new workspaces in Scalr. Set to True if the workspace is already created in Scalr.')
    parser.add_argument('--skip-backend-secrets', action='store_true', help='Whether to create shell variables (`SCALR_` and `TFC_`) in Scalr.')
    parser.add_argument('--skip-tfc-lock', action='store_true', help='Whether to skip locking of TFC/E workspaces')
    parser.add_argument('--management-env-name', type=str, default=DEFAULT_MANAGEMENT_ENV_NAME, help=f'Name of the management environment. Default: {DEFAULT_MANAGEMENT_ENV_NAME}')
    parser.add_argument('--disable-deletion-protection', action='store_true', help='Disable deletion protection in workspace resources. Default: enabled')
    parser.add_argument('--tfc-project', type=str, help='TFC project name to filter workspaces by')
    parser.add_argument('--skip-variables', type=str, help='Comma-separated list of variable keys to skip, or "*" to skip all variables')

    args = parser.parse_args()
    
    # Validate required arguments
    required_args = ['scalr_hostname', 'scalr_token', 'tfc_hostname', 'tfc_token', 'tfc_organization']
    missing_args = [arg for arg in required_args if not getattr(args, arg)]
    if missing_args:
        ConsoleOutput.error(f"Missing required arguments: {', '.join(missing_args)}")
        sys.exit(1)

    # Validate vcs_name if needed
    validate_vcs_name(args)

    # Convert argparse namespace to MigratorArgs and run migration
    migrator_args = MigratorArgs.from_argparse(args)
    migration_service = MigrationService(migrator_args)
    migration_service.migrate()

if __name__ == "__main__":
    main()
