#!/usr/bin/env python3
import json
import sys
import os
from typing import Any, Dict
import urllib.error
import urllib.request


class APIError(Exception):
    def __init__(self, error: urllib.error.HTTPError) -> None:
        self.code = error.code
        try:
            raw = error.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        self.api_error = self._message_from_http_error(error, raw)

    @staticmethod
    def _message_from_http_error(error: urllib.error.HTTPError, raw: str) -> str:
        text = raw.strip()
        if not text:
            reason = getattr(error, "reason", None) or ""
            return f"HTTP {error.code} {reason}".strip()
        try:
            body: Any = json.loads(text)
        except json.JSONDecodeError:
            snippet = text.replace("\n", " ")[:280]
            return f"HTTP {error.code} (response was not JSON): {snippet}"
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors:
            err0 = errors[0]
            if isinstance(err0, dict):
                return str(err0.get("detail") or err0.get("title") or err0.get("status") or text[:280])
            return str(err0)
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            return body["message"]
        return text[:500]

    def __str__(self) -> str:
        return str(self.api_error)


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

    def create_variable(self, key: str, var_value: str, workspace_id: str = "", var_set_id: str = "") -> Dict:
        if var_set_id:
            data = {
                "data": {
                    "type": "var-set-variables",
                    "attributes": {
                        "key": key,
                        "value": var_value,
                        "category": "shell",
                        "sensitive": True,
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

        if workspace_id:
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

        raise ValueError("Either workspace_id or var_set_id must be provided")


def main():
    input_data = json.load(sys.stdin)
    variables = input_data.get("variables")
    workspace_id = input_data.get("workspace_id", "")
    var_set_id = input_data.get("var_set_id", "")

    api_client = ScalrClient()
    created = 0
    exists = 0
    errors = []
    try:
        for variable in variables.split(",") if variables else []:
            value = os.environ.get(variable)
            if value is not None:
                api_client.create_variable(variable, value, workspace_id, var_set_id)
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
