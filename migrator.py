import binascii
import click
import hashlib
import json
import requests
import sys
from urllib.parse import urlencode

@click.group()
def cli():
    """
    Scripts helper.
    """


@cli.command()
@click.option(
    "--scalr-hostname",
    type=str,
    multiple=False,
    help="Scalr hostname",
)
@click.option(
    "--scalr-token",
    type=str,
    multiple=False,
    help="Scalr hostname",
)
@click.option(
    "--tf-hostname",
    type=str,
    multiple=False,
    help="Scalr hostname",
)
@click.option(
    "--tf-token",
    type=str,
    multiple=False,
    help="Scalr hostname",
)
@click.option(
    "-a",
    "--account-id",
    type=str,
    multiple=False,
    help="Scalr account",
)
@click.option(
    "-v",
    "--vcs_id",
    type=str,
    multiple=False,
    help="VCS identifier",
)
@click.option(
    "-l",
    "--lock",
    is_flag=True,
    multiple=False,
    help="Whether to lock TFE workspace",
)
def migrate(scalr_hostname, scalr_token, tf_hostname, tf_token, account_id, vcs_id, lock):
    def fetch_tfc(route, filters=None):
        if filters:
            filters = f"?{urlencode(filters)}"
        url = f"https://{tf_hostname}/api/v2/{route}{filters if filters else ''}"
        return requests.get(url, headers={"Authorization": f"Bearer {tf_token}"}).json()

    def write_scalr(route, data):
        response = requests.post(
            f"https://{scalr_hostname}/api/iacp/v3/{route}",
            headers={
                "Authorization": f"Bearer {scalr_token}",
                "Prefer": "profile=preview",
                "Content-Type": "application/vnd.api+json"
            },
            data=json.dumps(data)
        )

        if response.status_code != 201:
            print(data)
            print(response.json()["errors"][0])
            sys.exit(1)
        return response.json()

    def write_tfc(route, data):
        response = requests.post(
            f"https://{tf_hostname}/api/v2/{route}",
            headers={
                "Authorization": f"Bearer {tf_token}",
                "Content-Type": "application/vnd.api+json"
            },
            data=json.dumps(data)
        )

        if response.status_code not in [201, 200]:
            print(data)
            print(response.json()["errors"][0])
            sys.exit(1)
        return response.json()

    def create_environment(name: str, cost_enabled: bool):
        data = {
          "data": {
            "type": "environments",
            "attributes": {
              "name": name,
              "cost-estimation-enabled": cost_enabled
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

        return write_scalr("environments", data)

    def create_workspace(tf_workspace):
        attributes = tf_workspace["attributes"]
        vcs_repo = None
        relationships = {
            "environment": {
                "data": {
                    "type": "environments",
                    "id": env["data"]["id"]
                }
            }
        }
        if attributes["vcs-repo"]:
            branch = attributes["vcs-repo"]["branch"] if attributes["vcs-repo"]["branch"] else None
            vcs_repo = {
                "identifier": attributes["vcs-repo"]["identifier"],
                "branch":  branch,
                "dry-runs-enabled": attributes["speculative-enabled"],
                "trigger-prefixes": attributes["trigger-prefixes"]
            }
            relationships["vcs-provider"] = {
                "data": {
                    "type": "vcs-providers",
                    "id": vcs_id
                }
            }

        data = {
            "data": {
                "type": "workspaces",
                "attributes": {
                    "name": attributes["name"],
                    "auto-apply": attributes["auto-apply"],
                    "operations": attributes["operations"],
                    "terraform-version": attributes["terraform-version"],
                    "vcs-repo": vcs_repo,
                    "working-directory": attributes["working-directory"]
                },
                "relationships": relationships
            }
        }

        return write_scalr("workspaces", data)

    def create_state(tfc_state, workspace_id):
        attributes = tfc_state["attributes"]
        raw_state = requests.get(attributes["hosted-state-download-url"])
        encoded_state = binascii.b2a_base64(raw_state.content)
        decoded = binascii.a2b_base64(encoded_state)
        data = {
          "data": {
            "type":"state-versions",
            "attributes": {
              "serial": attributes["serial"],
              "md5": hashlib.md5(decoded).hexdigest(),
              "lineage": raw_state.json()["lineage"],
              "state": encoded_state.decode("utf-8")
            }
          }
        }

        response = requests.post(
            f"https://{scalr_hostname}/api/tfe/v2/workspaces/{workspace_id}/state-versions",
            headers={
                "Authorization": f"Bearer {scalr_token}",
                "Prefer": "profile=preview",
                "Content-Type": "application/vnd.api+json"
            },
            data=json.dumps(data)
        )

        if response.status_code != 201:
            print(data)
            print(response.json()["errors"][0])
            sys.exit(1)

        return response.json()

    def create_variable(key, value, category, sensitive, description=None, relationships=None):
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
                "relationships": relationships
            }
        }

        write_scalr("vars", data)

    def migrate_workspaces():
        def lock_tfc_workspace():
            if lock and not tf_workspace["attributes"]["locked"]:
                print(f"Locking {workspace_name}...")
                write_tfc(f"workspaces/{tf_workspace['id']}/actions/lock", {"reason": "Locked by migrator"})

        def migrate_state():
            state_filters = {
                "filter[workspace][name]": workspace_name,

                "filter[organization][name]": organization_name,
                "page[size]": 1
            }
            print("Migrating state...")
            for tf_state in fetch_tfc("state-versions", state_filters)["data"]:
                create_state(tf_state, workspace["data"]["id"])

        def migrate_variables():
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
                "filter[organization][name]": organization_name,
            }

            for api_var in fetch_tfc("vars", vars_filters)["data"]:
                attributes = api_var["attributes"]

                if not attributes["sensitive"]:
                    create_variable(
                        attributes["key"],
                        attributes["value"],
                        attributes["category"],
                        False,
                        attributes["description"],
                        relationships
                    )

            run = fetch_tfc(f"workspaces/{tf_workspace['id']}/runs", {"page[size]": 1})["data"]

            if run:
                plan = fetch_tfc(f"runs/{run[0]['id']}/plan/json-output")
                variables = plan["variables"]
                configuration_variables = plan["configuration"]["root_module"]["variables"]
                for var in configuration_variables:
                    if "sensitive" in configuration_variables[var]:
                        create_variable(
                            var,
                            variables[var]["value"],
                            "terraform",
                            True,
                            None,
                            relationships
                        )

        next_page = 1
        while True:
            workspace_filters = {
                "page[size]": 100,
                "page[number]": next_page,
            }

            workspaces = fetch_tfc(f"organizations/{organization_name}/workspaces", workspace_filters)
            next_page = workspaces["meta"]["pagination"]["next-page"]

            for tf_workspace in workspaces["data"]:
                workspace_name = tf_workspace["attributes"]["name"]
                print(f"Migrating workspace {workspace_name}...")
                workspace = create_workspace(tf_workspace)
                migrate_state()
                migrate_variables()
                lock_tfc_workspace()
                print(f"Migrating workspace {workspace_name}... Done")
            if not next_page:
                break

    account_relationships = {
        "account": {
            "data": {
                "type": "accounts",
                "id": account_id
            }
        }
    }

    print("Initializing backend secrets...")
    create_variable("SCALR_HOSTNAME", scalr_hostname, "shell", False, "Created by migrator", account_relationships)
    create_variable("SCALR_TOKEN", scalr_token, "shell", True, "Created by migrator", account_relationships)
    create_variable("TFE_HOSTNAME", tf_hostname, "shell", False, "Created by migrator", account_relationships)
    create_variable("TFE_TOKEN", tf_token, "shell", True, "Created by migrator", account_relationships)
    print("Initializing backend secrets... Done")

    for organization in fetch_tfc("organizations")["data"]:
        organization_name = organization["attributes"]["name"]
        print(f"Migrating organization {organization_name}...")
        env = create_environment(
            organization_name,
            organization["attributes"]["cost-estimation-enabled"]
        )
        migrate_workspaces()
        print(f"Migrating organization {organization_name}... Done")

    sys.exit(0)


if __name__ == "__main__":
    cli()
