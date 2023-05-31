import click
import json
import requests
import os
import sys
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

    def fetch_tfc(route, filters=None):
        response = requests.get(
            f"https://{hostname}/api/v2/{route}{encode_filters(filters)}",
            headers={"Authorization": f"Bearer {tf_token}"}
        )

        if response.status_code not in [200]:
            print(response.json()["errors"][0])
            sys.exit(1)
        return response.json()

    total_runs = 0
    organizations = fetch_tfc("organizations")
    for organization in organizations["data"]:
        name = organization["attributes"]["name"]
        workspaces = fetch_tfc(f"organizations/{name}/workspaces")
        for workspace in workspaces["data"]:
            ws_name = workspace["attributes"]["name"]
            runs = fetch_tfc(f"workspaces/{workspace['id']}/runs")
            ws_total = runs["meta"]["status-counts"]["total"]
            print(f"Workspace {ws_name} has had {ws_total} runs.")
            total_runs += ws_total
    print("---------------------------")
    print(f"The total runs count across all organizations: {total_runs}")
    sys.exit(0)


if __name__ == "__main__":
    cli()
