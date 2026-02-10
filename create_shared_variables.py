#!/usr/bin/env python3
"""
Script to create account-level variables in Scalr from TFC Variable Sets.
This creates shared variables accessible by all workspaces.

Usage:
    python create_shared_variables.py --varset-name "Shared Secrets"
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from typing import Dict, List, Optional


class APIClient:
    """Base API client."""

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
            try:
                error_json = json.loads(error_body)
                error_detail = error_json.get("errors", [{}])[0].get("detail", error_body)
            except:
                error_detail = error_body
            return {"error": True, "code": e.code, "detail": error_detail}


class TFCClient(APIClient):
    """Terraform Cloud API client."""

    def __init__(self, hostname: str, token: str):
        url = f"https://{hostname}/.well-known/terraform.json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req) as response:
            well_known = json.loads(response.read().decode("utf-8"))
            api_prefix = well_known.get("tfe.v2", "/api/v2/")
        super().__init__(hostname, token, api_prefix)

    def get_varsets(self, org_name: str) -> List[Dict]:
        """Get all variable sets in an organization."""
        return self.request(f"organizations/{org_name}/varsets").get("data", [])

    def get_varset_vars(self, varset_id: str) -> List[Dict]:
        """Get all variables in a variable set (handles pagination)."""
        all_vars = []
        page = 1
        while True:
            result = self.request(f"varsets/{varset_id}/relationships/vars?page[size]=100&page[number]={page}")
            data = result.get("data", [])
            all_vars.extend(data)
            
            # Check if there are more pages
            meta = result.get("meta", {})
            pagination = meta.get("pagination", {})
            if page >= pagination.get("total-pages", 1):
                break
            page += 1
        return all_vars


class ScalrClient(APIClient):
    """Scalr API client."""

    def __init__(self, hostname: str, token: str):
        super().__init__(hostname, token, "/api/iacp/v3/")
        self.account_id = self._get_account_id()

    def _get_account_id(self) -> str:
        """Get the account ID for the current token."""
        result = self.request("accounts")
        accounts = result.get("data", [])
        if not accounts:
            raise Exception("No account found for token")
        return accounts[0]["id"]

    def get_account_vars(self) -> List[Dict]:
        """Get account-level variables."""
        return self.request(f"vars?filter[account]={self.account_id}&filter[workspace]=null&filter[environment]=null").get("data", [])

    def create_account_variable(
        self,
        key: str,
        value: str,
        category: str,
        sensitive: bool,
        hcl: bool,
        description: str,
    ) -> Dict:
        """Create an account-level variable in Scalr."""
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
                    "account": {
                        "data": {"type": "accounts", "id": self.account_id}
                    }
                },
            }
        }
        return self.request("vars", method="POST", data=data)


def create_shared_variables(
    tfc_client: TFCClient,
    scalr_client: ScalrClient,
    tfc_org: str,
    varset_names: List[str],
    dry_run: bool = False,
) -> None:
    """Create account-level variables in Scalr from TFC variable sets."""
    
    # Get existing Scalr account variables
    print("Fetching existing Scalr account-level variables...")
    existing_vars = scalr_client.get_account_vars()
    existing_keys = {v["attributes"]["key"] for v in existing_vars}
    print(f"Found {len(existing_keys)} existing account-level variables\n")

    # Get TFC variable sets
    print("Fetching TFC variable sets...")
    all_varsets = tfc_client.get_varsets(tfc_org)
    
    all_vars = []
    for vs in all_varsets:
        vs_name = vs["attributes"]["name"]
        if vs_name in varset_names:
            print(f"  Loading '{vs_name}'...")
            vs_vars = tfc_client.get_varset_vars(vs["id"])
            print(f"    Found {len(vs_vars)} variables")
            all_vars.extend(vs_vars)

    print(f"\nTotal variables to create: {len(all_vars)}")
    
    if dry_run:
        print("\n=== DRY RUN - No changes will be made ===\n")

    created = 0
    skipped = 0
    errors = 0

    for var in all_vars:
        attrs = var["attributes"]
        key = attrs["key"]
        
        if key in existing_keys:
            print(f"  SKIP: {key} (already exists)")
            skipped += 1
            continue

        # Convert TFC category to Scalr category
        category = "shell" if attrs["category"] == "env" else attrs["category"]
        
        # For sensitive variables, use placeholder
        if attrs["sensitive"]:
            value = "PLACEHOLDER_FILL_ME_IN"
        else:
            value = attrs.get("value", "")

        if dry_run:
            print(f"  WOULD CREATE: {key} ({category}, sensitive={attrs['sensitive']})")
            created += 1
            continue

        result = scalr_client.create_account_variable(
            key=key,
            value=value,
            category=category,
            sensitive=attrs["sensitive"],
            hcl=attrs.get("hcl", False),
            description=attrs.get("description", "") or f"Migrated from TFC variable set",
        )

        if result.get("error"):
            print(f"  ERROR: {key} - {result.get('detail')}")
            errors += 1
        else:
            existing_keys.add(key)
            created += 1
            sens_label = " (sensitive - needs value)" if attrs["sensitive"] else ""
            print(f"  OK: {key}{sens_label}")

    print(f"\n=== Summary ===")
    print(f"Created: {created}")
    print(f"Skipped (existing): {skipped}")
    print(f"Errors: {errors}")
    
    if not dry_run and created > 0:
        print(f"\nNext steps:")
        print(f"1. Go to Scalr > Account Settings > Variables")
        print(f"2. Find variables with value 'PLACEHOLDER_FILL_ME_IN'")
        print(f"3. Update them with the correct values from your secrets manager")


def main():
    parser = argparse.ArgumentParser(description="Create Scalr account-level variables from TFC variable sets")
    parser.add_argument(
        "--varset-names",
        nargs="+",
        default=["Shared Secrets"],
        help="TFC variable set names to migrate",
    )
    parser.add_argument("--tfc-org", default="Huma", help="TFC organization name")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without creating variables")
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

    create_shared_variables(
        tfc_client=tfc_client,
        scalr_client=scalr_client,
        tfc_org=args.tfc_org,
        varset_names=args.varset_names,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
