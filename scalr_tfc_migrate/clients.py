"""HTTP clients for Terraform Cloud and Scalr APIs."""
import json
import os
import tarfile
import urllib.error
import urllib.request
from typing import Dict, List, Optional
from urllib.parse import urlencode

from scalr_tfc_migrate.args import MigratorArgs
from scalr_tfc_migrate.console import ConsoleOutput
from scalr_tfc_migrate.errors import APIError


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

    def get_by_short_url(self, short_url: str):
        return self.make_request(f"https://{self.hostname}/{short_url}")

    def download_cv(self, cv_url: str):
        return self.make_request(f"https://{self.hostname}/{cv_url}", decode=False)

    def make_request(self, url: str, method: str = "GET", data: Dict = None, headers: dict = None, decode: bool = True):
        if data:
            data = json.dumps(data).encode('utf-8')

        req = urllib.request.Request(url, data=data, method=method, headers=headers if headers else self.headers)

        try:
            with urllib.request.urlopen(req) as response:
                if response.code != 204:
                    r = response.read()
                    if not decode:
                        return r

                    return json.loads(r.decode('utf-8'))
                return {}
        except urllib.error.HTTPError as e:
            raise APIError(e)

    def get(self, route: str, filters: Optional[Dict] = None) -> Dict:
        url = f"https://{self.hostname}{self.api_version}{route}{self._encode_filters(filters)}"
        return self.make_request(url)

    def post(self, route: str, data: Dict) -> Dict:
        url = f"https://{self.hostname}{self.api_version}{route}"
        return self.make_request(url, method="POST", data=data)

    def patch(self, route: str, data: Dict) -> Dict:
        url = f"https://{self.hostname}{self.api_version}{route}"
        return self.make_request(url, method="PATCH", data=data)


class TFCClient(APIClient):
    def __init__(self, hostname: str, token: str):
        self.cached_version = None

        if not self.cached_version:
            url = f"https://{hostname}/.well-known/terraform.json"
            well_known = self.make_request(url, method="GET", headers={'Accept': "application/json"})
            self.cached_version = well_known.get("tfe.v2", "/api/v2/")

        super().__init__(hostname, token, self.cached_version)

    def init_backend_secrets(self, args: MigratorArgs):
        to_init = {
            "SCALR_HOSTNAME": args.scalr_hostname,
            "SCALR_TOKEN": args.scalr_token,
        }
        variables_set: List[Dict] = []

        for k, v in to_init.items():
            variables_set.append({
                "type": "vars",
                "attributes": {
                    "key": k,
                    "value": v,
                    "category": "env",
                    "sensitive": True,
                }
            })

        return self.create_variable_set(
            args.tfc_organization, args.credentials_set_name, variables_set
        )

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
                raise APIError(e)
            return None

    def get_workspace_vars(self, org_name: str, workspace_name: str) -> Dict:
        filters = {
            "filter[workspace][name]": workspace_name,
            "filter[organization][name]": org_name,
        }
        return self.get("vars", filters)

    def get_workspace(self, workspace_id: str) -> Dict:
        return self.get(f"workspaces/{workspace_id}")["data"]

    def list_variable_sets(self, org_name: str, page: int = 1) -> Dict:
        filters = {
            "page[size]": 100,
            "page[number]": page,
        }
        return self.get(f"organizations/{org_name}/varsets", filters)

    def get_variable_set(self, varset_id: str, filters: Optional[Dict] = None) -> Dict:
        return self.get(f"varsets/{varset_id}", filters)["data"]

    def get_variable_set_vars(self, varset_id: str, page: int = 1) -> Dict:
        filters = {
            "page[size]": 100,
            "page[number]": page,
        }
        return self.get(f"varsets/{varset_id}/relationships/vars", filters)

    def get_latest_plan(self, tf_workspace: dict, page_size: int = 1) -> Optional[Dict]:
        filters = {"page[size]": page_size}
        runs = self.get(f"workspaces/{tf_workspace['id']}/runs", filters)['data']
        plan = None
        latest_run = None
        if len(runs):
            latest_run = runs[0]
            ConsoleOutput.info(f"Found latest run {latest_run['id']} in status `{latest_run['attributes']['status']}`")
            plan = self.get_run_plan(latest_run['id'])

        if not plan:
            if latest_run:
                ConsoleOutput.info(
                    f"No plan found for {latest_run['id']}, trying to get the plan of the current state file"
                )
            state = self.get_current_state(tf_workspace)
            if state:
                plan = self.get_run_plan(state['relationships']['run']['data']['id'])

        return plan

    def get_current_state(self, tf_workspace: dict) -> Optional[Dict]:
        current_tf_state = tf_workspace["relationships"]["current-state-version"]

        if not current_tf_state or not current_tf_state.get("links"):
            ConsoleOutput.warning("State file is missing")
            return

        current_state_url = current_tf_state["links"]["related"]
        return self.get_by_short_url(current_state_url)["data"]

    def get_run_plan(self, run_id: str) -> Dict:
        try:
            return self.get(f"runs/{run_id}/plan/json-output")
        except Exception:
            ConsoleOutput.info("Skipping: plan file is unavailable")
            return {}

    def get_agent_pool(self, agent_pool_id: str) -> Optional[Dict]:
        """Get TFC agent pool details by ID."""
        try:
            return self.get(f"agent-pools/{agent_pool_id}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise APIError(e)
            return None

    def lock_workspace(self, workspace_id: str, reason: str) -> Dict:
        return self.post(f"workspaces/{workspace_id}/actions/lock", {"reason": reason})

    def create_variable_set(self, organization, name, variables_set: List[Dict]):
        if self.get(f"organizations/{organization}/varsets", {"q": name})["data"]:
            ConsoleOutput.info("Variable set already exists")
            return

        data = {
            "data": {
                "type": "varsets",
                "attributes": {
                    "name": name,
                    "global": True,
                    "priority": True,
                },
                "relationships": {
                    "vars": {
                        "data": variables_set
                    }
                }
            }
        }

        self.post(f"organizations/{organization}/varsets", data)

    def get_current_cv(self, tf_workspace: dict) -> Optional[str]:
        cv = self.get(f"workspaces/{tf_workspace['id']}/configuration-versions", {"page[size]": 1})['data']
        if not len(cv):
            return

        output_dir = "./terraform-cloud"
        os.makedirs(output_dir, exist_ok=True)
        content = self.download_cv(cv[0]['links']["download"])  # must be bytes
        with open(os.path.join(output_dir, f"{tf_workspace['id']}.tar.gz"), "wb") as f:
            f.write(content)

        tar_path = os.path.join(output_dir, f"{tf_workspace['id']}.tar.gz")
        extract_dir = os.path.join(output_dir, tf_workspace['id'])  # e.g., ./terraform-cloud/<workspace-id>/

        os.makedirs(extract_dir, exist_ok=True)
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(path=extract_dir, filter='fully_trusted')

        return os.path.join(extract_dir, tf_workspace['attributes']['working-directory'])


class ScalrClient(APIClient):
    def __init__(self, hostname: str, token: str):
        super().__init__(hostname, token, "/api/iacp/v3/")

    def init_backend_secrets(self, args: MigratorArgs):
        account_relationships = {
            "account": {
                "data": {
                    "type": "accounts",
                    "id": args.account_id
                }
            }
        }

        vars_to_create = {
            "SCALR_HOSTNAME": args.scalr_hostname,
            "SCALR_TOKEN": args.scalr_token,
            "TFE_HOSTNAME": args.tfc_hostname,
            "TFE_TOKEN": args.tfc_token,
        }

        for key in vars_to_create:
            vars_filters = {
                "filter[account]": args.account_id,
                "filter[key]": key,
                "filter[environment]": None
            }
            if self.get("vars", vars_filters)["data"]:
                continue
            try:
                self.create_variable(
                    key,
                    vars_to_create[key],
                    "shell",
                    True,
                    False,
                    "Created by migrator",
                    account_relationships
                )
            except APIError as e:
                if e.code == 422:
                    ConsoleOutput.info(f"Variable '{key}' already exists")
                    continue

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
                raise APIError(e)

    def get_workspace(self, environment_id, name: str) -> Optional[Dict]:
        try:
            response = self.get("workspaces", {"filter[name]": name, "filter[environment]": environment_id})
            workspaces = response.get("data", [])

            return workspaces[0] if workspaces else None
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise APIError(e)

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
        relationships = {
            "environment": {
                "data": {
                    "type": "environments",
                    "id": env_id,
                }
            }
        }

        if vcs_id:
            relationships["vcs-provider"] = {"data": {"type": "vcs-providers", "id": vcs_id}}
        if agent_pool_id:
            relationships["agent-pool"] = {"data": {"type": "agent-pools", "id": agent_pool_id}}

        filtered_attributes = {k: v for k, v in attributes.items() if v is not None}
        if "vcs-repo" in filtered_attributes and filtered_attributes["vcs-repo"]:
            filtered_attributes["vcs-repo"] = {
                k: v for k, v in filtered_attributes["vcs-repo"].items() if v is not None
            }

        data = {
            "data": {
                "type": "workspaces",
                "attributes": filtered_attributes,
                "relationships": relationships,
            }
        }

        if os.getenv("SCALR_DEBUG_ENABLED"):
            ConsoleOutput.debug(f"Creating workspace with payload: {json.dumps(data, indent=2)}")

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

    def get_var_sets(self, name: Optional[str] = None, page: int = 1) -> Dict:
        filters = {
            "page[size]": 100,
            "page[number]": page,
        }
        if name:
            filters["filter[name]"] = name
        return self.get("var-sets", filters)

    def create_var_set(
            self,
            name: str,
            description: Optional[str] = None,
            is_shared: bool = False,
            environment_ids: Optional[List[str]] = None
    ) -> Dict:
        relationships = {}
        if environment_ids:
            relationships["environments"] = {
                "data": [{"type": "environments", "id": env_id} for env_id in environment_ids]
            }

        data = {
            "data": {
                "type": "var-sets",
                "attributes": {
                    "name": name,
                    "description": description,
                    "is-shared": is_shared,
                },
                "relationships": relationships,
            }
        }
        return self.post("var-sets", data)

    def update_var_set(
            self,
            var_set_id: str,
            name: str,
            description: Optional[str] = None,
            is_shared: bool = False,
            environment_ids: Optional[List[str]] = None
    ) -> Dict:
        relationships = {}
        if environment_ids is not None:
            relationships["environments"] = {
                "data": [{"type": "environments", "id": env_id} for env_id in environment_ids]
            }

        data = {
            "data": {
                "type": "var-sets",
                "id": var_set_id,
                "attributes": {
                    "name": name,
                    "description": description,
                    "is-shared": is_shared,
                },
                "relationships": relationships,
            }
        }
        return self.patch(f"var-sets/{var_set_id}", data)

    def get_var_set(self, var_set_id: str, filters: Optional[Dict] = None) -> Dict:
        return self.get(f"var-sets/{var_set_id}", filters or {})

    def create_var_set_variable(
            self,
            var_set_id: str,
            key: str,
            value: str,
            category: str,
            sensitive: bool,
            is_hcl: bool = False,
            description: Optional[str] = None,
    ) -> Dict:
        data = {
            "data": {
                "type": "var-set-variables",
                "attributes": {
                    "key": key,
                    "value": value,
                    "category": category,
                    "sensitive": sensitive,
                    "description": description,
                    "hcl": is_hcl,
                },
                "relationships": {
                    "var-set": {
                        "data": {
                            "type": "var-sets",
                            "id": var_set_id,
                        }
                    }
                }
            }
        }
        return self.post("var-set-variables", data)

    def get_var_set_variables(self, var_set_id: str, key: Optional[str] = None, category: Optional[str] = None,
                              page: int = 1) -> Dict:
        filters = {
            "filter[var-set]": var_set_id,
            "page[size]": 100,
            "page[number]": page,
        }
        if key:
            filters["filter[key]"] = key
        if category:
            filters["filter[category]"] = category
        return self.get("var-set-variables", filters)

    def add_workspace_variable_sets(self, workspace_id: str, var_set_ids: List[str]) -> Dict:
        data = {
            "data": [{"type": "var-sets", "id": var_set_id} for var_set_id in var_set_ids]
        }
        return self.post(f"workspaces/{workspace_id}/relationships/var-sets", data)

    def get_current_state(self, workspace_id: str) -> Optional[Dict]:
        try:
            return self.get(f"workspaces/{workspace_id}/current-state-version")
        except APIError as e:
            if e.code != 404:
                raise e

