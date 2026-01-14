#!/usr/bin/env python3
import json
import sys
import os
from typing import Dict, Optional
import urllib.error
import urllib.request


class APIError(Exception):
    def __init__(self, error: urllib.error.HTTPError) -> None:
        errors: dict = json.loads(error.read().decode('utf-8'))["errors"][0]
        self.api_error = errors.get("detail", errors.get("title"))
        self.code = error.code

    def __str__(self) -> str:
        return self.api_error


class APIClient:
    def __init__(self, hostname: str, token: str):
        self.hostname = hostname
        self.token = token
        self.api_version = "/api/iacp/v3/"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.api+json",
        }

    def make_request(self, url: str, method: str = "GET", data: Dict = None, headers: dict = None) -> Dict:
        if data:
            data = json.dumps(data).encode('utf-8')

        req = urllib.request.Request(url, data=data, method=method, headers=headers if headers else self.headers)

        try:
            with urllib.request.urlopen(req) as response:
                if response.code != 204:
                    return json.loads(response.read().decode('utf-8'))
                return {}
        except urllib.error.HTTPError as e:
            raise APIError(e)

    def post(self, route: str, data: Dict) -> Dict:
        url = f"https://{self.hostname}{self.api_version}{route}"
        return self.make_request(url, method="POST", data=data)


class ScalrClient(APIClient):
    def __init__(self):
        hostname = os.getenv("SCALR_HOSTNAME")
        token = os.getenv("SCALR_TOKEN")

        if not hostname:
            raise ValueError("SCALR_HOSTNAME must be set")
        if not token:
            raise ValueError("SCALR_TOKEN must be set")

        super().__init__(hostname, token)

    def create_variable(self, key: str, var_value: str, workspace_id: str) -> Dict:
        data = {
            "data": {
                "type": "vars",
                "attributes": {
                    "key": key,
                    "value": var_value,
                    "category": "shell",
                    "sensitive": True,
                },
                "relationships": {
                    "workspace": {
                        "data": {
                            "type": "workspaces",
                            "id": workspace_id,
                        }
                    }
                }
            }
        }

        return self.post("vars", data)


def main():
    input_data = json.load(sys.stdin)
    variables = input_data.get("variables")
    workspace_id = input_data.get("workspace_id")

    api_client = ScalrClient()
    created = 0
    exists = 0
    errors = []
    try:
        for variable in variables.split(",") if variables else []:
            value = os.environ.get(variable)
            if value is not None:
                api_client.create_variable(variable, value, workspace_id)
                created += 1
    except APIError as e:
        errors.append(str(e))

    status = "ok"
    if created and len(errors) > 0:
        status = "warning"
    elif not created and len(errors) > 0:
        status = "error"

    print(json.dumps({"created": str(created), "failed": str(len(errors)), "status": status, "errors": "\n".join(errors)}, indent=2))


if __name__ == "__main__":
    main()
