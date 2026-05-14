"""API and domain errors."""
import json
import urllib.error
from typing import Any


class MissingDataError(Exception):
    pass


class MissingMappingError(Exception):
    pass


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
