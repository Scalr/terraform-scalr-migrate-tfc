import click
import json
import math
import os
import requests
import sys
import time
from urllib.parse import urlencode


@click.group()
def cli():
    """
    Scripts helper.
    """


@cli.command()
@click.option(
    "--hostname",
    default="app.terraform.io",
    type=str,
    multiple=False,
    help="TFC/E hostname",
)
def list_all(hostname):
    continue_message = f"Run the `terraform login {hostname}` command to continue"
    home_dir = os.getenv("HOME")
    try:
        credentials = open(f"{home_dir}/.terraform.d/credentials.tfrc.json")
    except Exception:
        print(f"Cannot locate the credentials file. {continue_message}")
        sys.exit(1)

    credentials_json: dict = json.loads(credentials.read())
    credentials.close()

    if not credentials_json["credentials"].get(hostname, None):
        print(f"Cannot find credentials for the Terraform Cloud/Enterprise. {continue_message}")
        sys.exit(1)

    tf_token = credentials_json["credentials"][hostname]["token"]

    def encode_filters(filters):
        return f"?{urlencode(filters)}" if filters else ""

    def fetch_tfc(route, filters=None, retry_attempt=0):
        response = requests.get(
            f"https://{hostname}/api/v2/{route}{encode_filters(filters)}",
            headers={"Authorization": f"Bearer {tf_token}"}
        )

        status_code = response.status_code
        if status_code not in [200]:
            if status_code == 401:
                print(f"The token is expired or invalid. {continue_message}")
                sys.exit(1)
            elif status_code == 429:
                if retry_attempt <= 10:
                    retry_attempt += 1
                    retry_in = math.ceil(float(response.headers.get('x-ratelimit-reset', 60)))
                    print(f"API rate limited, retrying in {retry_in} seconds, attempt #{retry_attempt}")
                    time.sleep(retry_in)
                    return fetch_tfc(route, filters, retry_attempt)
                else:
                    print("API rate limited, the maximum number of attempts exceeded")
                    sys.exit(1)
            else:
                print(response.json()["errors"][0])
                sys.exit(1)
        return response.json()

    def fetch_organizations(page_number=1):
        return fetch_tfc("organizations",  [('page[size]', '100'), ('page[number]', page_number)])

    def fetch_workspaces(org_name, page_number=1):
        return fetch_tfc(f"organizations/{org_name}/workspaces", [('page[size]', '100'), ('page[number]', page_number)])

    total_runs = 0
    organizations_token = 1

    while organizations_token:
        organizations = fetch_organizations(organizations_token)
        organizations_token = organizations["meta"]["pagination"]["next-page"]

        for organization in organizations["data"]:
            name = organization["attributes"]["name"]
            workspaces_token = 1
            while workspaces_token:
                workspaces = fetch_workspaces(name, workspaces_token)
                workspaces_token = workspaces["meta"]["pagination"]["next-page"]
                for workspace in workspaces["data"]:
                    ws_name = workspace["attributes"]["name"]
                    ws_total = fetch_tfc(f"workspaces/{workspace['id']}/runs")["meta"]["status-counts"]["total"]
                    print(f"Workspace {name}/{ws_name} has had {ws_total} runs.")
                    total_runs += ws_total
    print("---------------------------")
    print(f"The total runs count across all organizations: {total_runs}")
    sys.exit(0)


if __name__ == "__main__":
    cli()
