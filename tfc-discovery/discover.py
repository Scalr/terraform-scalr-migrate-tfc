"""Entrypoint for TFC/E pre-migration discovery.

Reuses the migrator's TFCClient/ConsoleOutput/error handling from
scalr_tfc_migrate (see scalr_tfc_migrate/discovery.py) rather than
duplicating an HTTP client here, so auth precedence and pagination stay
consistent with migrate.sh. Since this script lives in its own folder
(tfc-discovery/), the repo root is added to sys.path below so the
scalr_tfc_migrate package - which lives one level up - is importable.
"""
import os
import sys

if sys.version_info < (3, 12):
    sys.exit("Python 3.12 or higher is required")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scalr_tfc_migrate.discovery import main

if __name__ == "__main__":
    main()
