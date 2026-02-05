#!/usr/bin/env python3
"""
Script to migrate TFC workspace variables and variable sets to Scalr.

Usage:
    python migrate_variables.py --workspace-name dev --scalr-workspace-id ws-xxx
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Any


class APIClient:
    """Base API client for TFC and Scalr."""

    def __init__(self, hostname: str, token: str, api_prefix: str):
        self.hostname = hostname
        self.token = token
        self.api_prefix = api_prefix
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.api+json",
        }

    def request(
        self, endpoint: str, method: str = "GET", data: Optional[Dict] = None
    ) -> Dict:
        """Make an API request."""
        url = f"https://{self.hostname}{self.api_prefix}{endpoint}"
        
        body = None
        if data:
            body = json.dumps(data).encode("utf-8")

        req = urllib.request.Request(url, data=body, method=method, headers=self.headers)

        try:
            with urllib.request.urlopen(req) as response:
                if response.status == 204:
                    return {}
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8")
            print(f"API Error {e.code}: {error_body}")
            raise


class TFCClient(APIClient):
    """Terraform Cloud API client."""

    def __init__(self, hostname: str, token: str):
        # Get the API version from well-known endpoint
        url = f"https://{hostname}/.well-known/terraform.json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req) as response:
            well_known = json.loads(response.read().decode("utf-8"))
            api_prefix = well_known.get("tfe.v2", "/api/v2/")
        super().__init__(hostname, token, api_prefix)

    def get_workspace_vars(self, org_name: str, workspace_name: str) -> List[Dict]:
        """Get variables for a workspace."""
        endpoint = f"vars?filter[workspace][name]={workspace_name}&filter[organization][name]={org_name}"
        return self.request(endpoint).get("data", [])

    def get_varsets(self, org_name: str) -> List[Dict]:
        """Get all variable sets in an organization."""
        return self.request(f"organizations/{org_name}/varsets").get("data", [])

    def get_varset_vars(self, varset_id: str) -> List[Dict]:
        """Get variables in a variable set."""
        return self.request(f"varsets/{varset_id}/relationships/vars").get("data", [])

    def get_varset_workspaces(self, varset_id: str) -> List[Dict]:
        """Get workspaces attached to a variable set."""
        return self.request(f"varsets/{varset_id}/relationships/workspaces").get("data", [])


class ScalrClient(APIClient):
    """Scalr API client."""

    def __init__(self, hostname: str, token: str):
        super().__init__(hostname, token, "/api/iacp/v3/")

    def get_workspace_vars(self, workspace_id: str) -> List[Dict]:
        """Get variables for a workspace."""
        return self.request(f"vars?filter[workspace]={workspace_id}").get("data", [])

    def create_variable(
        self,
        key: str,
        value: str,
        category: str,
        sensitive: bool,
        hcl: bool,
        description: str,
        workspace_id: str,
    ) -> Dict:
        """Create a variable in Scalr."""
        data = {
            "data": {
                "type": "vars",
                "attributes": {
                    "key": key,
                    "value": value,
                    "category": category,
                    "sensitive": sensitive,
                    "hcl": hcl,
                    "description": description or "",
                },
                "relationships": {
                    "workspace": {
                        "data": {"type": "workspaces", "id": workspace_id}
                    }
                },
            }
        }
        return self.request("vars", method="POST", data=data)


def migrate_variables(
    tfc_client: TFCClient,
    scalr_client: ScalrClient,
    tfc_org: str,
    workspace_name: str,
    scalr_workspace_id: str,
    include_varsets: List[str],
) -> None:
    """Migrate variables from TFC to Scalr."""
    
    # Get existing Scalr variables to avoid duplicates
    existing_vars = scalr_client.get_workspace_vars(scalr_workspace_id)
    existing_keys = {v["attributes"]["key"] for v in existing_vars}
    print(f"Found {len(existing_keys)} existing variables in Scalr workspace")

    # Get workspace variables from TFC
    print(f"\nFetching workspace variables from TFC...")
    tfc_vars = tfc_client.get_workspace_vars(tfc_org, workspace_name)
    print(f"Found {len(tfc_vars)} workspace variables in TFC")

    # Get variable set variables if specified
    varset_vars = []
    if include_varsets:
        print(f"\nFetching variable sets from TFC...")
        all_varsets = tfc_client.get_varsets(tfc_org)
        for vs in all_varsets:
            vs_name = vs["attributes"]["name"]
            if vs_name in include_varsets or vs["attributes"].get("global"):
                print(f"  - Loading variables from '{vs_name}'...")
                vs_vars = tfc_client.get_varset_vars(vs["id"])
                varset_vars.extend(vs_vars)
                print(f"    Found {len(vs_vars)} variables")

    # Combine all variables
    all_vars = tfc_vars + varset_vars
    print(f"\nTotal variables to migrate: {len(all_vars)}")

    # Migrate each variable
    migrated = 0
    skipped = 0
    sensitive_skipped = []

    for var in all_vars:
        attrs = var["attributes"]
        key = attrs["key"]
        
        if key in existing_keys:
            print(f"  SKIP: {key} (already exists)")
            skipped += 1
            continue

        # Convert TFC category to Scalr category
        category = "shell" if attrs["category"] == "env" else attrs["category"]
        
        if attrs["sensitive"]:
            # Can't get sensitive values from TFC API
            sensitive_skipped.append(key)
            print(f"  SKIP: {key} (sensitive - requires manual entry)")
            continue

        try:
            scalr_client.create_variable(
                key=key,
                value=attrs.get("value", ""),
                category=category,
                sensitive=attrs["sensitive"],
                hcl=attrs.get("hcl", False),
                description=attrs.get("description", ""),
                workspace_id=scalr_workspace_id,
            )
            existing_keys.add(key)
            migrated += 1
            print(f"  OK: {key}")
        except Exception as e:
            print(f"  ERROR: {key} - {e}")

    print(f"\n=== Migration Summary ===")
    print(f"Migrated: {migrated}")
    print(f"Skipped (existing): {skipped}")
    print(f"Skipped (sensitive): {len(sensitive_skipped)}")
    
    if sensitive_skipped:
        print(f"\nSensitive variables requiring manual entry:")
        for key in sensitive_skipped:
            print(f"  - {key}")


def main():
    parser = argparse.ArgumentParser(description="Migrate TFC variables to Scalr")
    parser.add_argument("--workspace-name", required=True, help="TFC workspace name")
    parser.add_argument("--scalr-workspace-id", required=True, help="Scalr workspace ID")
    parser.add_argument("--tfc-org", default="Huma", help="TFC organization name")
    parser.add_argument(
        "--include-varsets",
        nargs="+",
        default=["Dev Secrets", "Shared Secrets"],
        help="Variable sets to include",
    )
    args = parser.parse_args()

    # Get credentials from environment
    tfc_token = os.environ.get("TFC_TOKEN")
    scalr_token = os.environ.get("SCALR_TOKEN")
    scalr_hostname = os.environ.get("SCALR_HOSTNAME", "humaai.scalr.io")
    tfc_hostname = os.environ.get("TFC_HOSTNAME", "app.terraform.io")

    if not tfc_token or not scalr_token:
        print("Error: TFC_TOKEN and SCALR_TOKEN environment variables required")
        sys.exit(1)

    tfc_client = TFCClient(tfc_hostname, tfc_token)
    scalr_client = ScalrClient(scalr_hostname, scalr_token)

    migrate_variables(
        tfc_client=tfc_client,
        scalr_client=scalr_client,
        tfc_org=args.tfc_org,
        workspace_name=args.workspace_name,
        scalr_workspace_id=args.scalr_workspace_id,
        include_varsets=args.include_varsets,
    )


if __name__ == "__main__":
    main()
