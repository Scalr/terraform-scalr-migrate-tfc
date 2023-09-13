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


def validate_vcs_id_set(ctx, param, value):
    # if not skip_workspace_creation than vcs_id must be set.
    if not ctx.params.get("skip_workspace_creation") and not value:
        raise click.BadParameter(
            f"If --skip-workspace-creation flag is not set, a valid vcs_id must be passed."
        )
    return value


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
    "--scalr-environment",
    type=str,
    multiple=False,
    required=False,
    help="Optional. Scalr environment to create. By default it takes TFC/E organization name.",
)
@click.option(
    "--tf-hostname",
    type=str,
    multiple=False,
    help="TFC/E hostname",
)
@click.option(
    "--tf-token",
    type=str,
    multiple=False,
    help="TFC/E token",
)
@click.option(
    "--tf-organization",
    type=str,
    multiple=False,
    help="TFC/E organization name",
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
    "--vcs-id",
    type=str,
    multiple=False,
    help="VCS identifier",
    callback=validate_vcs_id_set,
)
@click.option(
    "-w",
    "--workspaces",
    type=str,
    multiple=False,
    help="Workspaces to migrate. By default - all",
)
@click.option(
    "--skip-workspace-creation",
    is_flag=True,
    multiple=False,
    help="Whether to create new workspaces in Scalr. Set to True if the workspace is already created in Scalr.",
)
@click.option(
    "--skip-backend-secrets",
    is_flag=True,
    multiple=False,
    help="Whether to create shell variables (`SCALR_` and `TFC_`) in Scalr.",
)
@click.option(
    "-l",
    "--lock",
    is_flag=True,
    multiple=False,
    help="Whether to lock TFE workspace",
)
def migrate(
    scalr_hostname,
    scalr_token,
    scalr_environment,
    tf_hostname,
    tf_token,
    tf_organization,
    account_id,
    vcs_id,
    workspaces,
    skip_workspace_creation,
    skip_backend_secrets,
    lock
):
    def encode_filters(filters):
        encoded = ''
        if filters:
            encoded = f"?{urlencode(filters)}"
        return encoded

    def fetch_tfc(route, filters=None):
        response = requests.get(
            f"https://{tf_hostname}/api/v2/{route}{encode_filters(filters)}",
            headers={"Authorization": f"Bearer {tf_token}"}
        )

        if response.status_code not in [200]:
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

    def fetch_scalr(route, filters=None):
        response = requests.get(
            f"https://{scalr_hostname}/api/iacp/v3/{route}{encode_filters(filters)}",
            headers={"Authorization": f"Bearer {scalr_token}", "Prefer": "profile=preview"}
        )

        if response.status_code not in [200]:
            print(response.json()["errors"][0])
            sys.exit(1)
        return response.json()

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

        if response.status_code not in [201]:
            print(data)
            print(response.json()["errors"][0])
            sys.exit(1)
        return response.json()

    def create_environment(cost_enabled: bool):
        data = {
          "data": {
            "type": "environments",
            "attributes": {
              "name": scalr_environment,
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
        relationships = {
            "environment": {
                "data": {
                    "type": "environments",
                    "id": env["id"]
                }
            },
            "vcs-provider": {
                "data": {
                    "type": "vcs-providers",
                    "id": vcs_id
                }
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
                    "vcs-repo": {
                        "identifier": attributes["vcs-repo"]["display-identifier"],
                        "branch":  attributes["vcs-repo"]["branch"] if attributes["vcs-repo"]["branch"] else None,
                        "dry-runs-enabled": attributes["speculative-enabled"],
                        "trigger-prefixes": attributes["trigger-prefixes"]
                    },
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
            "type": "state-versions",
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

    def create_variable(variable_key, value, category, sensitive, description=None, relationships=None):
        data = {
            "data": {
                "type": "vars",
                "attributes": {
                    "key": variable_key,
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
                "filter[organization][name]": tf_organization,
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
                "filter[organization][name]": tf_organization,
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
            if not run:
                return

            plan = fetch_tfc(f"runs/{run[0]['id']}/plan/json-output")
            if "variables" not in plan:
                return

            variables = plan["variables"]
            root_module = plan["configuration"]["root_module"]

            configuration_variables = root_module["variables"] if "variables" in root_module else []

            for var in configuration_variables:
                if "sensitive" not in configuration_variables[var]:
                    continue
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

            tfc_workspaces = fetch_tfc(f"organizations/{tf_organization}/workspaces", workspace_filters)
            next_page = tfc_workspaces["meta"]["pagination"]["next-page"]

            for tf_workspace in tfc_workspaces["data"]:
                workspace_name = tf_workspace["attributes"]["name"]
                if workspace_name not in workspaces and "*" not in workspaces:
                    continue

                workspace_exists = fetch_scalr(
                    "workspaces",
                    {"filter[name]": workspace_name, "filter[environment]": env["id"]},
                )["data"]
                # workspace must exist if skip_workspace_creation
                # workspace must not exist if not skip_workspace_creation
                if len(workspace_exists) ^ skip_workspace_creation:
                    continue

                if not skip_workspace_creation:
                    if not tf_workspace["attributes"]["vcs-repo"]:
                        continue

                    print(f"Migrating workspace {workspace_name}...")
                    workspace = create_workspace(tf_workspace)
                else:
                    workspace = {"data": workspace_exists[0]}

                migrate_state()
                migrate_variables()
                lock_tfc_workspace()
                print(f"Migrating workspace {workspace_name}... Done")
            if not next_page:
                break

    def init_backend_secrets():
        print(skip_backend_secrets)
        if skip_backend_secrets:
            return

        account_relationships = {
            "account": {
                "data": {
                    "type": "accounts",
                    "id": account_id
                }
            }
        }

        vars_to_create = {
            "SCALR_HOSTNAME": scalr_hostname,
            "SCALR_TOKEN": scalr_token,
            "TFE_HOSTNAME": tf_hostname,
            "TFE_TOKEN": tf_token,
        }

        print("Initializing backend secrets...")
        for key in vars_to_create:
            vars_filters = {"filter[account]": account_id, "filter[key]": key, "filter[environment]": None}
            if fetch_scalr("vars", vars_filters)["data"]:
                continue
            print(f"Missing shell variable `{key}`. Creating...")
            create_variable(key, vars_to_create[key], "shell", True, "Created by migrator", account_relationships)
        print("Initializing backend secrets... Done")

    init_backend_secrets()
    organization = fetch_tfc(f"organizations/{tf_organization}")["data"]
    if not scalr_environment:
        scalr_environment = tf_organization

    env = fetch_scalr("environments", {"query": scalr_environment})["data"]
    if len(env):
        env = env[0]
    else:
        print(f"Migrating organization {tf_organization}...")
        env = create_environment(organization["attributes"]["cost-estimation-enabled"])["data"]

    workspaces = workspaces.split(',')
    migrate_workspaces()
    print(f"Migrating organization {tf_organization} ({scalr_environment})... Done")

    sys.exit(0)


if __name__ == "__main__":
    cli()
