"""--workspace-defaults: Scalr workspace settings that TFC/E has no counterpart for."""
import json
import os
from typing import Any, Dict, List, Optional

from scalr_tfc_migrate.errors import InvalidInputError
from scalr_tfc_migrate.matching import matches_workspace_patterns

# scalr_workspace attribute -> (API attribute, accepted type). The SSH key is not a workspace
# attribute in the API: it is linked once the workspace exists.
SUPPORTED_ATTRIBUTES = {
    "type": ("environment-type", str),
    "auto_queue_runs": ("auto-queue-runs", str),
    "run_operation_timeout": ("run-operation-timeout", int),
    "force_latest_run": ("force-latest-run", bool),
    "remote_backend": ("remote-backend", bool),
    "var_files": ("var-files", list),
    "ssh_key_id": (None, str),
}


class WorkspaceDefaults:
    """
    Settings applied to every workspace a run creates, with optional overrides by workspace name:

        {
          "ssh_key_id": "ssh-...",
          "auto_queue_runs": "never",
          "overrides": [{"workspace": "prod-*", "type": "production"}]
        }

    Overrides apply in order, so a later one wins. Keys starting with "_" are comments.
    Workspaces that already exist in Scalr are left as they are.
    """

    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self.defaults: Dict[str, Any] = {}
        self.overrides: List[Dict[str, Any]] = []
        if path:
            self.load(path)

    def load(self, path: str) -> None:
        if not os.path.exists(path):
            raise InvalidInputError(f"Workspace defaults file '{path}' does not exist")
        try:
            with open(path, "r") as f:
                document = json.load(f)
        except json.JSONDecodeError as e:
            raise InvalidInputError(f"Workspace defaults file '{path}' is not valid JSON: {e}")
        if not isinstance(document, dict):
            raise InvalidInputError(f"Workspace defaults file '{path}' must be a JSON object")

        overrides = document.get("overrides") or []
        if not isinstance(overrides, list):
            raise InvalidInputError(f"'overrides' in workspace defaults file '{path}' must be a list")

        self.defaults = self._validated({k: v for k, v in document.items() if k != "overrides"})
        for override in overrides:
            if not isinstance(override, dict) or not override.get("workspace"):
                raise InvalidInputError(
                    f"Every override in workspace defaults file '{path}' needs a 'workspace' pattern: {override}"
                )
            attributes = self._validated({k: v for k, v in override.items() if k != "workspace"})
            self.overrides.append({"workspace": override["workspace"], **attributes})

    def _validated(self, attributes: Dict[str, Any]) -> Dict[str, Any]:
        result = {}
        for key, value in attributes.items():
            if key.startswith("_"):
                continue
            if key not in SUPPORTED_ATTRIBUTES:
                raise InvalidInputError(
                    f"Unsupported setting '{key}' in workspace defaults file '{self.path}'. "
                    f"Supported: {', '.join(SUPPORTED_ATTRIBUTES)}"
                )
            expected = SUPPORTED_ATTRIBUTES[key][1]
            # bool is an int in Python, but not a valid timeout.
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise InvalidInputError(
                    f"'{key}' in workspace defaults file '{self.path}' must be a {expected.__name__}"
                )
            result[key] = value
        return result

    def for_workspace(self, name: str) -> Dict[str, Any]:
        settings = dict(self.defaults)
        for override in self.overrides:
            if matches_workspace_patterns(name, override["workspace"]):
                settings.update({k: v for k, v in override.items() if k != "workspace"})
        return settings

    def ssh_key_ids(self) -> List[str]:
        """Every SSH key the file names, so a dry run can check they exist."""
        values = [self.defaults.get("ssh_key_id")] + [override.get("ssh_key_id") for override in self.overrides]
        return sorted({value for value in values if value})

    @staticmethod
    def api_attributes(settings: Dict[str, Any]) -> Dict[str, Any]:
        return {SUPPORTED_ATTRIBUTES[key][0]: value for key, value in settings.items()
                if SUPPORTED_ATTRIBUTES[key][0]}
