"""Machine-readable results: the per-run JSON report and the cumulative workspace map."""
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from scalr_tfc_migrate import constants
from scalr_tfc_migrate.args import MigratorArgs

# Never written to a report: they are credentials.
REDACTED_ARGS = {"scalr_token", "tfc_token"}

STATUS_PENDING = "pending"
STATUS_MIGRATED = "migrated"
STATUS_EXISTING = "existing"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_PLANNED = "planned"  # --dry-run
STATUS_VERIFIED = "verified"  # --verify, no mismatch
STATUS_MISMATCH = "mismatch"  # --verify


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MigrationReport:
    """
    Collects what a run did to every workspace in scope, so that a script or an agent driving the
    migrator can read the outcome instead of parsing console output. Values of variables are never
    recorded, only their keys.
    """

    def __init__(self, args: MigratorArgs, mode: str) -> None:
        self.args = args
        self.mode = mode
        self.started_at = utc_now()
        self.workspaces: Dict[str, Dict] = {}
        self.variable_sets: Dict[str, Dict] = {}
        self.environment_access_policies: List[Dict] = []
        # Mode-specific facts about the run as a whole (destination environment, management workspace, ...).
        self.details: Dict = {}
        self.warnings: List[str] = []
        self.blockers: List[str] = []
        # The entry of the workspace being migrated; hooks deep in the call chain record into it.
        self.current: Optional[Dict] = None

    def workspace(self, name: str, tfc_id: Optional[str] = None) -> Dict:
        entry = self.workspaces.get(name)
        if entry is None:
            entry = {
                "name": name,
                "tfc_id": tfc_id,
                "status": STATUS_PENDING,
                "error": None,
                "scalr_id": None,
                "created": False,
                "terraform_version": None,
                "state": None,
                "variables": [],
                "provider_configuration": None,
                "agent_pool": None,
                "remote_state_consumers": [],
                "access_policies": [],
                "run_triggers": [],
                "tfc_locked": None,
                "warnings": [],
            }
            self.workspaces[name] = entry
        if tfc_id:
            entry["tfc_id"] = tfc_id
        return entry

    def begin_workspace(self, name: str, tfc_id: str) -> Dict:
        self.current = self.workspace(name, tfc_id)
        return self.current

    def end_workspace(self) -> None:
        self.current = None

    def add_variable(self, key: str, category: str, sensitive: bool, action: str,
                     reason: Optional[str] = None) -> None:
        if self.current is None:
            return
        self.current["variables"].append({
            "key": key,
            "category": category,
            "sensitive": sensitive,
            "action": action,
            "reason": reason,
        })

    def set_variable(self, key: str, action: str, reason: Optional[str] = None) -> None:
        """Update a variable recorded earlier, e.g. a sensitive one recovered after the first pass."""
        if self.current is None:
            return
        for variable in self.current["variables"]:
            if variable["key"] == key:
                variable["action"] = action
                variable["reason"] = reason
                return

    def set_pending_variables(self, category: str, action: str, reason: str) -> None:
        if self.current is None:
            return
        for variable in self.current["variables"]:
            if variable["category"] == category and variable["action"] == "pending":
                variable["action"] = action
                variable["reason"] = reason

    def warn(self, message: str, entry: Optional[Dict] = None) -> None:
        entry = entry if entry is not None else self.current
        if entry is not None:
            entry["warnings"].append(message)
        else:
            self.warnings.append(message)

    def block(self, message: str) -> None:
        if message not in self.blockers:
            self.blockers.append(message)

    def variable_set(self, name: str, tfc_id: Optional[str] = None) -> Dict:
        if name not in self.variable_sets:
            self.variable_sets[name] = {"name": name, "tfc_id": tfc_id, "warnings": []}
        return self.variable_sets[name]

    def failed(self) -> List[str]:
        return [name for name, entry in self.workspaces.items() if entry["status"] == STATUS_FAILED]

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for entry in self.workspaces.values():
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        return counts

    def arguments(self) -> Dict:
        arguments = asdict(self.args)
        for key in REDACTED_ARGS:
            if arguments.get(key):
                arguments[key] = "***"
        if arguments.get("workspace_names") is not None:
            arguments["workspace_names"] = sorted(arguments["workspace_names"])
        return arguments

    def to_dict(self) -> Dict:
        return {
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "arguments": self.arguments(),
            "summary": self.summary(),
            "blockers": self.blockers,
            "details": self.details,
            "warnings": self.warnings,
            "workspaces": list(self.workspaces.values()),
            "variable_sets": list(self.variable_sets.values()),
            "environment_access_policies": self.environment_access_policies,
        }

    def write(self, output_dir: str) -> str:
        path = self.args.report_file
        if not path:
            # Milliseconds: runs a script starts back to back can finish within the same second,
            # and the names still have to sort in the order the runs happened.
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            path = os.path.join(output_dir, "reports", f"{self.mode}-{timestamp}.json")

        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        return path


class WorkspaceMap:
    """
    TFC workspace → Scalr workspace, accumulated over every run (wave) and every destination
    environment. It lets a run resolve workspaces migrated by earlier runs (remote state consumers,
    run triggers), and it is what tooling needs to repoint `terraform_remote_state` / `tfe_outputs`
    readers at Scalr.
    """

    def __init__(self, path: str = constants.WORKSPACE_MAP_FILE) -> None:
        self.path = path
        self.document: Dict = {"version": 1, "workspaces": {}}
        if os.path.exists(path):
            with open(path, "r") as f:
                self.document = json.load(f)
            self.document.setdefault("workspaces", {})

    @staticmethod
    def key(tfc_hostname: str, tfc_organization: str, workspace_name: str) -> str:
        return f"{tfc_hostname}/{tfc_organization}/{workspace_name}"

    def entries(self) -> List[Dict]:
        return list(self.document["workspaces"].values())

    def get_by_tfc_id(self, tfc_id: str) -> Optional[Dict]:
        for entry in self.document["workspaces"].values():
            if entry["tfc"]["id"] == tfc_id:
                return entry
        return None

    def record(self, tfc: Dict, scalr: Dict) -> None:
        key = self.key(tfc["hostname"], tfc["organization"], tfc["workspace"])
        previous = self.document["workspaces"].get(key) or {}
        self.document["workspaces"][key] = {
            "tfc": tfc,
            "scalr": scalr,
            "migrated_at": previous.get("migrated_at") or utc_now(),
            "updated_at": utc_now(),
        }

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.document, f, indent=2, sort_keys=True)
