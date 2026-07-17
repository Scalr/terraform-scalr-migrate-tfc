"""Regression tests for APIClient's request-timeout/network-error handling.

Run from the repo root (scalr_tfc_migrate must be importable):
    pip install pytest
    python3 -m pytest tfc-discovery/tests/test_clients_network_error.py -v

Without a socket timeout, urllib.request.urlopen blocks forever on a stalled
connection with zero feedback - this is what made discover.sh look like it
was hanging. These tests confirm timeouts/connection failures now raise a
clear scalr_tfc_migrate.errors.NetworkError instead, and that ordinary HTTP
error responses still raise APIError as before (unaffected by the fix).
"""
import urllib.error

import pytest

from scalr_tfc_migrate import errors
from scalr_tfc_migrate.clients import APIClient


def _client():
    return APIClient("example.com", "token")


def test_timeout_raises_network_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(errors.NetworkError):
        _client().get("workspaces")


def test_connection_failure_raises_network_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(errors.NetworkError):
        _client().get("workspaces")


def test_http_error_still_raises_api_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("https://example.com", 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(errors.APIError):
        _client().get("workspaces")


def test_timeout_is_passed_to_urlopen(monkeypatch):
    seen = {}

    class FakeResponse:
        code = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _client().get("workspaces")
    assert seen["timeout"] is not None  # a real timeout is set, not None (which means "block forever")
