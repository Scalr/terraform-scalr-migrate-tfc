import click
import json
import math
import os
import requests
import sys
import time

from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone


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
@click.option(
    "--period",
    default=None,
    type=int,
    multiple=False,
    help="Period to check in days.",
)
def list_all(hostname: str, period: int = None):
    if period:
        print(f"Checking runs within the last {period} days\n")
    else:
        print(f"Checking runs within the last year\n")

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

    def encode_filters(filters):
        return f"?{urlencode(filters)}" if filters else ""

    def fetch_tfc(route, filters=None, retry_attempt=0):
        url = f"https://{hostname}/api/v2/{route}{encode_filters(filters)}"
        response = requests.get(url, headers={
            "Authorization": f"Bearer {credentials_json["credentials"][hostname]["token"]}"
        })

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
        return fetch_tfc("organizations", [('page[size]', '100'), ('page[number]', page_number)])

    total_runs = 0
    organizations_token = 1

    filter_runs = [('filter[status_group]', 'final'), ('page[size]', 100)]
    report = "Organization %s has had %d runs"
    check_period = None

    if period:
        check_period = datetime.now() - timedelta(days=period)
        check_period = check_period.replace(tzinfo=timezone.utc)

    while organizations_token:
        organizations = fetch_organizations(organizations_token)
        organizations_token = organizations["meta"]["pagination"]["next-page"]

        for organization in organizations["data"]:
            org_total = 0
            name = organization["attributes"]["name"]
            runs_token = 1
            while runs_token:
                runs = fetch_tfc(f"organizations/{name}/runs", filter_runs + [('page[number]', runs_token)])
                runs_token = runs["meta"]["pagination"]["next-page"]
                for run in runs["data"]:
                    created_at_str = run["attributes"]["created-at"]
                    created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))

                    if check_period is None or created_at >= check_period:
                        org_total += 1
                    else:
                        break

            print(report % (name, org_total))
            total_runs += org_total

    print("---------------------------")
    print(f"The total runs count across all organizations: {total_runs}")
    sys.exit(0)


if __name__ == "__main__":
    cli()
