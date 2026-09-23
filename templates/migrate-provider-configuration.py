#!/usr/bin/env python3
"""
Runs inside a TFC/E remote plan and creates a Scalr provider configuration.

TFC never returns the value of a sensitive variable through its API, so provider
credentials cannot be read by the migrator. They are, however, present in the environment
of a TFC run, which is where this script executes: it is invoked by the `external` data
source of export-provider-configuration.tf while the plan is running in TFC.

Everything it needs is passed in the payload built by the migrator; the credentials
themselves are read from the environment and sent straight to the Scalr API.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode


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
            formatted = "; ".join(APIError._format_error(e) for e in errors[:3])
            if formatted:
                return formatted
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            return body["message"]
        return text[:500]

    @staticmethod
    def _format_error(error: Any) -> str:
        """A JSON:API error is useless without the field it points at ("Field required.")."""
        if not isinstance(error, dict):
            return str(error)
        detail = str(error.get("detail") or error.get("title") or error.get("status") or "").strip()
        source = error.get("source") or {}
        field = source.get("pointer") or source.get("parameter") if isinstance(source, dict) else None
        if field:
            return f"{detail} [{field}]" if detail else f"[{field}]"
        return detail

    def __str__(self) -> str:
        return str(self.api_error)


class ScalrClient:
    def __init__(self) -> None:
        self.hostname = os.getenv("SCALR_HOSTNAME")
        token = os.getenv("SCALR_TOKEN")

        if not self.hostname:
            raise ValueError("SCALR_HOSTNAME must be set")
        if not token:
            raise ValueError("SCALR_TOKEN must be set")

        self.api_version = "/api/iacp/v3/"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.api+json",
        }

    def make_request(self, url: str, method: str = "GET", data: Optional[Dict] = None) -> Dict:
        payload = json.dumps(data).encode('utf-8') if data else None
        req = urllib.request.Request(url, data=payload, method=method, headers=self.headers)

        try:
            with urllib.request.urlopen(req) as response:
                if response.code != 204:
                    return json.loads(response.read().decode('utf-8'))
                return {}
        except urllib.error.HTTPError as e:
            raise APIError(e)

    def get(self, route: str, filters: Optional[Dict] = None) -> Dict:
        query = f"?{urlencode(filters)}" if filters else ""
        return self.make_request(f"https://{self.hostname}{self.api_version}{route}{query}")

    def post(self, route: str, data: Dict) -> Dict:
        return self.make_request(f"https://{self.hostname}{self.api_version}{route}", method="POST", data=data)

    def patch(self, route: str, data: Dict) -> Dict:
        return self.make_request(f"https://{self.hostname}{self.api_version}{route}", method="PATCH", data=data)

    def find_provider_configuration(self, name: str) -> Optional[Dict]:
        configurations = self.get("provider-configurations", {"filter[name]": name}).get("data", [])
        for configuration in configurations:
            if configuration.get("attributes", {}).get("name") == name:
                return configuration
        return None

    def create_provider_configuration(self, account_id: str, attributes: Dict, environment_ids: List[str]) -> str:
        relationships = {"account": {"data": {"type": "accounts", "id": account_id}}}
        if environment_ids:
            relationships["environments"] = {
                "data": [{"type": "environments", "id": env_id} for env_id in environment_ids]
            }

        data = {
            "data": {
                "type": "provider-configurations",
                "attributes": attributes,
                "relationships": relationships,
            }
        }
        return self.post("provider-configurations", data)["data"]["id"]

    def update_provider_configuration(self, pc_id: str, attributes: Dict) -> str:
        data = {
            "data": {
                "type": "provider-configurations",
                "id": pc_id,
                "attributes": attributes,
            }
        }
        self.patch(f"provider-configurations/{pc_id}", data)
        return pc_id

    def create_parameter(self, pc_id: str, attributes: Dict) -> None:
        data = {
            "data": {
                "type": "provider-configuration-parameters",
                "attributes": attributes,
            }
        }
        self.post(f"provider-configurations/{pc_id}/parameters", data)


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(access_key: str, secret_key: str, session_token: Optional[str], host: str,
                  region: str, service: str, query: str, amz_date: str) -> Dict[str, str]:
    """Sign a GET request the way AWS SigV4 requires, with nothing but the standard library."""
    date = amz_date[:8]
    headers = {"host": host, "x-amz-date": amz_date}
    if session_token:
        headers["x-amz-security-token"] = session_token

    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = (f"GET\n/\n{query}\n{canonical_headers}\n{signed_headers}\n"
                         f"{hashlib.sha256(b'').hexdigest()}")
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = (f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
                      f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}")

    key = _sign(f"AWS4{secret_key}".encode("utf-8"), date)
    for part in (region, service, "aws4_request"):
        key = _sign(key, part)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                f"SignedHeaders={signed_headers}, Signature={signature}")
    return headers


def resolve_aws_account_id() -> Optional[str]:
    """
    sts:GetCallerIdentity with the credentials of this run. It is allowed for every caller,
    needs no IAM permission and no SDK, and answers the one thing TFC never stores: which
    AWS account these credentials belong to.
    """
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        return None

    host = "sts.amazonaws.com"
    query = "Action=GetCallerIdentity&Version=2011-06-15"
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = sigv4_headers(access_key, secret_key, os.environ.get("AWS_SESSION_TOKEN"),
                            host, "us-east-1", "sts", query, amz_date)

    request = urllib.request.Request(f"https://{host}/?{query}", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    match = re.search(r"<Account>(\d+)</Account>", body)
    return match.group(1) if match else None


def resolve_identity(payload: Dict) -> Optional[str]:
    if payload.get("resolve_identity") == "aws-sts":
        return resolve_aws_account_id()
    return None


def decode_payload(raw: str) -> Dict:
    """The payload is base64 encoded so that it survives being passed as a -var value."""
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return json.loads(raw)


def resolve_entry(payload: Dict, keys: List[str]) -> Dict:
    """The parameters-file entry for this cloud account, when it was not matched locally."""
    entries = payload.get("entries") or {}
    for key in keys:
        entry = entries.get(key.strip().lower())
        if entry:
            return entry
    return {}


def resolve_name(payload: Dict, entry: Dict, keys: List[str]) -> Optional[str]:
    """The real cloud identity is tried first, the variable value second."""
    if payload.get("name"):
        return payload["name"]
    if entry.get("name"):
        return entry["name"]
    if keys and payload.get("name_template"):
        return payload["name_template"].format(provider=payload.get("provider_name", ""), key=keys[0].strip())
    return None


def build_attributes(payload: Dict, entry: Dict, name: str) -> Dict:
    attributes: Dict = {"name": name, "provider-name": payload["provider_name"]}

    # Credentials are only available here, in the environment of the TFC run.
    for attribute, env_names in (payload.get("attribute_env") or {}).items():
        for env_name in env_names:
            value = os.environ.get(env_name)
            if value:
                attributes[attribute] = value
                break

    attributes.update(payload.get("static_attributes") or {})
    # What the entry says wins: the file is the operator's decision, the rest is inference.
    attributes.update(payload.get("extra_attributes") or {})
    attributes.update(entry.get("attributes") or {})

    for flag in ("is-shared", "export-shell-variables", "is-custom"):
        value = entry.get(flag) if entry.get(flag) is not None else payload.get(flag)
        if value is not None:
            attributes[flag] = value

    return {k: v for k, v in attributes.items() if v is not None}


def main() -> None:
    query = json.load(sys.stdin)
    result = {"status": "error", "name": "", "id": "", "key": "", "message": ""}
    sent_attributes: List[str] = []

    try:
        payload = decode_payload(query.get("payload") or "")
        key_env = payload.get("key_env")
        key_value = os.environ.get(key_env) if key_env else None

        identity = resolve_identity(payload)
        keys = [k for k in (identity, key_value) if k]
        result["key"] = identity or key_value or ""

        entry = resolve_entry(payload, keys)
        name = resolve_name(payload, entry, keys)
        if not name:
            known = ", ".join(keys) or "nothing that identifies the cloud account"
            result["message"] = (
                f"No entry in the parameters file matches {known}"
            )
            print(json.dumps(result))
            return

        result["name"] = name
        attributes = build_attributes(payload, entry, name)
        sent_attributes = sorted(attributes)

        missing = [a for a in payload.get("required") or [] if not attributes.get(a)]
        if missing:
            result["message"] = (
                f"The TFC run environment does not provide values for: {', '.join(missing)}"
            )
            print(json.dumps(result))
            return

        client = ScalrClient()
        existing = client.find_provider_configuration(name)

        if existing and not payload.get("update_existing"):
            result.update({"status": "exists", "id": existing["id"],
                           "message": "provider configuration already exists"})
            print(json.dumps(result))
            return

        if existing:
            pc_id = client.update_provider_configuration(existing["id"], attributes)
            result.update({"status": "updated", "id": pc_id})
        else:
            pc_id = client.create_provider_configuration(
                payload["account_id"], attributes,
                entry.get("environment_ids") or payload.get("environment_ids") or []
            )
            result.update({"status": "created", "id": pc_id})

            for parameter in entry.get("parameters") or payload.get("parameters") or []:
                client.create_parameter(pc_id, parameter)

        print(json.dumps(result))
    except APIError as e:
        # Values are never reported, but the attribute names tell whether something was missing.
        sent = ", ".join(sent_attributes)
        result["message"] = f"{e} (attributes sent: {sent})" if sent else str(e)
        print(json.dumps(result))
    except Exception as e:  # the plan must not fail because of this data source
        result["message"] = f"{type(e).__name__}: {e}"
        print(json.dumps(result))


if __name__ == "__main__":
    main()
