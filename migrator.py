import binascii
import argparse
import fnmatch
import hashlib
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode
from typing import Dict, List, Optional, Union
import os
from datetime import datetime
from dataclasses import dataclass
import time

# Constants
MAX_TERRAFORM_VERSION = "1.5.7"
DEFAULT_MANAGEMENT_ENV_NAME = "terraform-management"
DEFAULT_MANAGEMENT_WORKSPACE_NAME = "workspace-management"
RATE_LIMIT_DELAY = 5  # seconds
MAX_RETRIES = 3

class RateLimitError(Exception):
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
    data: Optional[Union[str, bytes]] = None,
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
        except Exception as e:
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
    vcs_id: Optional[str]
    workspaces: str
    skip_workspace_creation: bool
    skip_backend_secrets: bool
    lock: bool
    management_env_name: str = DEFAULT_MANAGEMENT_ENV_NAME
    management_workspace_name: str = DEFAULT_MANAGEMENT_WORKSPACE_NAME

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
            vcs_id=args.vcs_id,
            workspaces=args.workspaces or "*",
            skip_workspace_creation=args.skip_workspace_creation,
            skip_backend_secrets=args.skip_backend_secrets,
            lock=args.lock,
            management_env_name=args.management_env_name,
            management_workspace_name=args.management_workspace_name
        )

    def get_or_create_environment(self) -> str:
        """Get existing environment or create a new one."""
        # First try to find existing environment
        url = f"https://{self.scalr_hostname}/api/iacp/v3/environments"
        headers = {
            "Authorization": f"Bearer {self.scalr_token}",
            "Prefer": "return=minimal"
        }
        
        try:
            response = make_request(url, headers=headers)
            environments = json.loads(response.read().decode())
            
            for env in environments.get("data", []):
                if env["attributes"]["name"] == self.management_env_name:
                    return env["id"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        
        # Create new environment if not found
        data = {
            "data": {
                "type": "environments",
                "attributes": {
                    "name": self.management_env_name,
                    "account-id": self.account_id
                }
            }
        }
        
        response = make_request(
            url,
            method="POST",
            headers=headers,
            data=json.dumps(data).encode()
        )
        return json.loads(response.read().decode())["data"]["id"]

    def get_or_create_workspace(self, environment_id: str) -> str:
        """Get existing workspace or create a new one."""
        # First try to find existing workspace
        url = f"https://{self.scalr_hostname}/api/iacp/v3/workspaces"
        headers = {
            "Authorization": f"Bearer {self.scalr_token}",
            "Prefer": "return=minimal"
        }
        
        try:
            response = make_request(url, headers=headers)
            workspaces = json.loads(response.read().decode())
            
            for ws in workspaces.get("data", []):
                if ws["attributes"]["name"] == self.management_workspace_name:
                    return ws["id"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        
        # Create new workspace if not found
        data = {
            "data": {
                "type": "workspaces",
                "attributes": {
                    "name": self.management_workspace_name,
                    "environment-id": environment_id,
                    "vcs-provider-id": self.vcs_id,
                    "terraform-version": MAX_TERRAFORM_VERSION,
                    "working-directory": "generated_terraform"
                }
            }
        }
        
        response = make_request(
            url,
            method="POST",
            headers=headers,
            data=json.dumps(data).encode()
        )
        return json.loads(response.read().decode())["data"]["id"]

class TerraformResource:
    def __init__(self, resource_type: str, name: str, attributes: Dict):
        self.resource_type = resource_type
        self.name = name
        self.attributes = attributes
        self.id = None

    def to_hcl(self) -> str:
        attrs = []
        for key, value in self.attributes.items():
            if isinstance(value, str):
                attrs.append(f'  {key} = "{value}"')
            elif isinstance(value, bool):
                attrs.append(f'  {key} = {str(value).lower()}')
            elif isinstance(value, dict):
                attrs.append(f'  {key} = {json.dumps(value)}')
            else:
                attrs.append(f'  {key} = {value}')
        
        return f'resource "{self.resource_type}" "{self.name}" {{\n{chr(10).join(attrs)}\n}}'

    def to_import_command(self) -> str:
        if not self.id:
            return None
        return f'terraform import {self.resource_type}.{self.name} {self.id}'

class ResourceManager:
    def __init__(self):
        self.resources: List[TerraformResource] = []
        self.import_commands: List[str] = []

    def add_resource(self, resource: TerraformResource):
        self.resources.append(resource)
        if resource.id:
            import_cmd = resource.to_import_command()
            if import_cmd:
                self.import_commands.append(import_cmd)

    def write_resources(self, output_dir: str):
        os.makedirs(output_dir, exist_ok=True)
        
        # Write main.tf
        with open(os.path.join(output_dir, "main.tf"), "w") as f:
            f.write("# Generated by Scalr Migrator\n")
            f.write(f"# Generated at: {datetime.now().isoformat()}\n\n")
            for resource in self.resources:
                f.write(resource.to_hcl() + "\n\n")

        # Write import script
        with open(os.path.join(output_dir, "import_commands.sh"), "w") as f:
            f.write("#!/bin/bash\n\n")
            f.write("# Generated by Scalr Migrator\n")
            f.write(f"# Generated at: {datetime.now().isoformat()}\n\n")
            f.write("set -e\n\n")
            
            # Step 1: Initialize local backend
            f.write("echo 'Step 1: Initializing local backend...'\n")
            f.write("cat > backend.tf << 'EOL'\n")
            f.write('''terraform {
  backend "local" {
    path = "terraform.tfstate"
  }
}
''')
            f.write("EOL\n")
            f.write("terraform init\n\n")
            
            # Step 2: Prepare state by importing resources locally
            f.write("echo 'Step 2: Importing resources to local state...'\n")
            for cmd in self.import_commands:
                f.write(f"{cmd}\n")
            f.write("\n")
            
            # Step 3: Initialize remote backend
            f.write("echo 'Step 3: Initializing remote backend...'\n")
            f.write("mv backend.tf backend.tf.local\n")
            f.write("cp backend.tf.remote backend.tf\n")
            f.write("terraform init -migrate-state\n\n")
            
            # Step 4: Push state to remote workspace
            f.write("echo 'Step 4: Pushing state to remote workspace...'\n")
            f.write("terraform push\n\n")
            
            f.write("echo 'Migration completed successfully!'\n")

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

    def _make_request(self, url: str, method: str = "GET", data: Dict = None) -> Dict:
        if data:
            data = json.dumps(data).encode('utf-8')
        
        req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
        
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            print(f"\r\nURL: {url}\r\nResponse: {error_body}")
            sys.exit(1)

    def get(self, route: str, filters: Optional[Dict] = None) -> Dict:
        url = f"https://{self.hostname}/api/{self.api_version}/{route}{self._encode_filters(filters)}"
        return self._make_request(url)

    def post(self, route: str, data: Dict) -> Dict:
        url = f"https://{self.hostname}/api/{self.api_version}/{route}"
        return self._make_request(url, method="POST", data=data)

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
        super().__init__(hostname, token, "v3")

    def get_environment(self, name: str) -> Dict:
        return self.get("environments", {"query": name})

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

    def create_workspace(self, env_id: str, attributes: Dict = {}, vcs_id: str | None = None) -> Dict:
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
            data["data"]["relationships"]["vcs-provider"]["data"]["id"] = vcs_id

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

    def create_variable(self, key: str, value: str, category: str, sensitive: bool, 
                       description: Optional[str] = None, relationships: Optional[Dict] = None) -> Dict | None:
        
        data = {
            "data": {
                "type": "vars",
                "attributes": {
                    "key": key,
                    "value": value,
                    "category": category,
                    "sensitive": sensitive,
                    "description": description
                },
                "relationships": relationships or {}
            }
        }
        return self.post("vars", data)

class MigrationService:
    def __init__(self, args: MigratorArgs):
        self.args: MigratorArgs = args
        self.resource_manager: ResourceManager = ResourceManager()
        self.tfc: TFCClient = TFCClient(args.tf_hostname, args.tf_token)
        self.scalr: ScalrClient = ScalrClient(args.scalr_hostname, args.scalr_token)

    def create_environment(self, name: str) -> Dict:
        response = self.scalr.create_environment(name, self.args.account_id)
        
        # Create Terraform resource
        env_resource = TerraformResource(
            "scalr_environment",
            f"env_{name.lower().replace('-', '_')}",
            {
                "name": name,
                "account_id": self.args.account_id
            }
        )
        env_resource.id = response["data"]["id"]
        self.resource_manager.add_resource(env_resource)
        
        return response

    def create_workspace(self, tf_workspace: Dict, env_id: str) -> Dict:
        attributes = tf_workspace["attributes"]
        
        # Enforce max Terraform version
        if attributes["terraform-version"] > MAX_TERRAFORM_VERSION:
            print(f"Warning: Workspace {attributes['name']} uses Terraform {attributes['terraform-version']}. "
                  f"Downgrading to {MAX_TERRAFORM_VERSION}")
            attributes["terraform-version"] = MAX_TERRAFORM_VERSION

        workspace_attrs = {
            "name": attributes["name"],
            "auto-apply": attributes["auto-apply"],
            "operations": attributes["operations"],
            "terraform-version": attributes["terraform-version"],
            "vcs-repo": {
                "identifier": attributes["vcs-repo"]["display-identifier"],
                "branch": attributes["vcs-repo"]["branch"] if attributes["vcs-repo"]["branch"] else None,
                "dry-runs-enabled": attributes["speculative-enabled"],
                "trigger-prefixes": attributes["trigger-prefixes"]
            },
            "working-directory": attributes["working-directory"]
        }

        response = self.scalr.create_workspace(env_id, workspace_attrs, self.args.vcs_id)
        
        # Create Terraform resource
        workspace_resource = TerraformResource(
            "scalr_workspace",
            f"ws_{attributes['name'].lower().replace('-', '_')}",
            {
                "name": attributes["name"],
                "auto_apply": attributes["auto-apply"],
                "operations": attributes["operations"],
                "terraform_version": attributes["terraform-version"],
                "vcs_repo": {
                    "identifier": attributes["vcs-repo"]["display-identifier"],
                    "branch": attributes["vcs-repo"]["branch"],
                    "dry_runs_enabled": attributes["speculative-enabled"],
                    "trigger_prefixes": attributes["trigger-prefixes"]
                },
                "working_directory": attributes["working-directory"],
                "environment_id": env_id,
                "vcs_provider_id": self.args.vcs_id
            }
        )
        workspace_resource.id = response["data"]["id"]
        self.resource_manager.add_resource(workspace_resource)
        
        return response

    def create_state(self, tfc_state: Dict, workspace_id: str) -> Dict:
        attributes = tfc_state["attributes"]
        raw_state = self.tfc._make_request(attributes["hosted-state-download-url"])
        encoded_state = binascii.b2a_base64(raw_state.content)
        decoded = binascii.a2b_base64(encoded_state)
        
        state_attrs = {
            "serial": attributes["serial"],
            "md5": hashlib.md5(decoded).hexdigest(),
            "lineage": raw_state.json()["lineage"],
            "state": encoded_state.decode("utf-8")
        }

        return self.scalr.create_state_version(workspace_id, state_attrs)

    def create_backend_config(self, env_id: str, workspace_id: str) -> None:
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
        output_dir = "generated_terraform"
        os.makedirs(output_dir, exist_ok=True)
        
        with open(os.path.join(output_dir, "backend.tf.remote"), "w") as f:
            f.write("# Generated by Scalr Migrator\n")
            f.write(f"# Generated at: {datetime.now().isoformat()}\n\n")
            f.write(backend_config)

    def migrate_workspace(self, tf_workspace: Dict, env_id: str):
        workspace_name = tf_workspace["attributes"]["name"]
        
        # Check if workspace exists
        workspace_exists = self.scalr.get("workspaces", {
            "filter[name]": workspace_name,
            "filter[environment]": env_id
        })["data"]
        
        if len(workspace_exists) ^ self.args.skip_workspace_creation:
            return

        if not self.args.skip_workspace_creation:
            if not tf_workspace["attributes"]["vcs-repo"]:
                return

            print(f"Migrating workspace {workspace_name}...")
            workspace = self.create_workspace(tf_workspace, env_id)
        else:
            workspace = {"data": workspace_exists[0]}

        # Migrate state
        state_filters = {
            "filter[workspace][name]": workspace_name,
            "filter[organization][name]": self.args.tf_organization,
            "page[size]": 1
        }
        print("Migrating state...")
        for tf_state in self.tfc.get("state-versions", state_filters)["data"]:
            self.create_state(tf_state, workspace["data"]["id"])

        # Migrate variables
        print("Migrating variables...")
        relationships = {
            "workspace": {
                "data": {
                    "type": "workspaces",
                    "id": workspace["data"]["id"]
                }
            }
        }

        vars_filters = {
            "filter[workspace][name]": workspace_name,
            "filter[organization][name]": self.args.tf_organization,
        }

        for api_var in self.tfc.get_workspace_vars(self.args.tf_organization, workspace_name)["data"]:
            attributes = api_var["attributes"]
            if not attributes["sensitive"]:
                self.scalr.create_variable(
                    attributes["key"],
                    attributes["value"],
                    attributes["category"],
                    False,
                    attributes["description"],
                    relationships
                )
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

        print(f"Migrating workspace {workspace_name}... Done")

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
                json.dump(credentials, f, indent=2)
            print("Credentials added successfully.")
        else:
            print(f"Credentials for {self.args.scalr_hostname} already exist in {credentials_file}")

    def migrate(self):
        self.init_backend_secrets()
        
        # Get organization and create environment
        organization = self.tfc.get_organization(self.args.tf_organization)["data"]
        if not self.args.scalr_environment:
            self.args.scalr_environment = self.args.tf_organization

        env = self.scalr.get_environment(self.args.scalr_environment)["data"]
        if not env:
            print(f"Migrating organization {self.args.tf_organization}...")
            env = self.create_environment(
                self.args.scalr_environment
            )["data"]
        else:
            env = env[0]

        # Create management environment and workspace
        print("Creating management environment and workspace...")
        management_env = self.create_environment(self.args.management_env_name)["data"]
        
        management_workspace_attrs = {
            "name": self.args.management_workspace_name,
            "auto-apply": False,
            "operations": True,
            "terraform-version": MAX_TERRAFORM_VERSION,
        }

        management_workspace = self.scalr.create_workspace(
            management_env["id"], 
            management_workspace_attrs,
            self.args.vcs_id
        )["data"]

        # Create backend configuration for the management workspace
        print("Creating backend configuration for management workspace...")
        self.create_backend_config(management_env["id"], management_workspace["id"])

        # Migrate workspaces
        next_page = 1
        while True:
            tfc_workspaces = self.tfc.get_workspaces(self.args.tf_organization, next_page)
            next_page = tfc_workspaces["meta"]["pagination"]["next-page"]

            for tf_workspace in tfc_workspaces["data"]:
                workspace_name = tf_workspace["attributes"]["name"]
                if not self.should_migrate_workspace(workspace_name):
                    print(f"Skipping workspace {workspace_name}...")
                    continue

                self.migrate_workspace(tf_workspace, env["id"])

            if not next_page:
                break

        # Write generated Terraform resources and import commands
        output_dir = "generated_terraform"
        self.resource_manager.write_resources(output_dir)
        print(f"\nGenerated Terraform resources and import commands in directory: {output_dir}")
        print(f"Migrating organization {self.args.tf_organization} ({self.args.scalr_environment})... Done")
        
        # Check and update Terraform credentials
        self.check_and_update_credentials()
        
        # Print instructions for importing and pushing state
        print("\nNext steps to complete the migration:")
        print("1. Navigate to the generated_terraform directory:")
        print("   cd generated_terraform")
        print("\n2. Make the import script executable and run it:")
        print("   chmod +x import_commands.sh")
        print("   ./import_commands.sh")
        print("\nNote: The script will:")
        print("   - Initialize a local backend")
        print("   - Import all resources to the local state")
        print("   - Migrate the state to the remote backend")
        print("   - Push the state to Scalr")
        print("\nCredentials have been automatically configured in ~/.terraform.d/credentials.tfrc.json")

def validate_vcs_id(args: argparse.Namespace) -> None:
    if not args.skip_workspace_creation and not args.vcs_id:
        print("Error: If --skip-workspace-creation flag is not set, a valid vcs_id must be passed.")
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
    parser.add_argument('-v', '--vcs-id', type=str, help='VCS identifier')
    parser.add_argument('-w', '--workspaces', type=str, help='Workspaces to migrate. By default - all')
    parser.add_argument('--skip-workspace-creation', action='store_true', help='Whether to create new workspaces in Scalr. Set to True if the workspace is already created in Scalr.')
    parser.add_argument('--skip-backend-secrets', action='store_true', help='Whether to create shell variables (`SCALR_` and `TFC_`) in Scalr.')
    parser.add_argument('-l', '--lock', action='store_true', help='Whether to lock TFE workspace')
    parser.add_argument('--management-env-name', type=str, default=DEFAULT_MANAGEMENT_ENV_NAME, help=f'Name of the management environment. Default: {DEFAULT_MANAGEMENT_ENV_NAME}')
    parser.add_argument('--management-workspace-name', type=str, default=DEFAULT_MANAGEMENT_WORKSPACE_NAME, help=f'Name of the management workspace. Default: {DEFAULT_MANAGEMENT_WORKSPACE_NAME}')

    args = parser.parse_args()
    
    # Validate required arguments
    required_args = ['scalr_hostname', 'scalr_token', 'tf_hostname', 'tf_token', 'tf_organization', 'account_id']
    missing_args = [arg for arg in required_args if not getattr(args, arg)]
    if missing_args:
        print(f"Error: Missing required arguments: {', '.join(missing_args)}")
        sys.exit(1)
    
    # Validate vcs_id if needed
    validate_vcs_id(args)

    # Convert argparse namespace to MigratorArgs and run migration
    migrator_args = MigratorArgs.from_argparse(args)
    migration_service = MigrationService(migrator_args)
    migration_service.migrate()

if __name__ == "__main__":
    main()
