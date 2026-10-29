"""--inventory: export the TFC/E side of a migration as reviewable CSV (or TSV) and JSON files."""
import json
import os
from typing import Dict, List, Optional, Set, Tuple

from scalr_tfc_migrate import errors
from scalr_tfc_migrate.args import MigratorArgs
from scalr_tfc_migrate.clients import TFCClient
from scalr_tfc_migrate.console import ConsoleOutput
from scalr_tfc_migrate.matching import matches_workspace_patterns
from scalr_tfc_migrate.report import utc_now
from scalr_tfc_migrate.tables import write_table

# Variables that usually hold cloud credentials, which a Scalr provider configuration can hold instead.
CREDENTIAL_PREFIXES = ("ARM_", "AWS_", "GOOGLE_", "TFC_AWS_", "TFC_AZURE_", "TFC_GCP_")
# Custom permissions of a TFC team on a workspace, reported next to the access level.
TEAM_WORKSPACE_PERMISSIONS = ("runs", "variables", "state-versions", "sentinel-mocks", "workspace-locking",
                              "run-tasks")

WORKSPACE_COLUMNS = [
    "name", "id", "project", "vcs_repo", "vcs_branch", "working_directory", "terraform_version",
    "execution_mode", "agent_pool", "auto_apply", "global_remote_state", "locked", "resource_count",
    "has_state", "last_run_status", "last_run_at", "latest_change_at", "variable_count",
    "sensitive_variables", "credential_variables", "variable_sets", "remote_state_consumers",
    "inbound_run_triggers",
]
# scalr_team and roles are left empty for a person to fill in, so the table can be passed back
# with --team-access.
TEAM_ACCESS_COLUMNS = ["scope", "project", "workspace", "team", "team_id", "access", "scalr_team", "roles",
                       *TEAM_WORKSPACE_PERMISSIONS]
# sharing_mode is `global` (any workspace of the organization may read the state; the consumers
# are not known, so the row has none) or `explicit`. The repository and working directory of
# both sides are what a change to the consumers' code needs to find it.
STATE_CONSUMER_COLUMNS = ["producer", "producer_id", "producer_vcs_repo", "producer_working_directory",
                          "sharing_mode", "consumer", "consumer_id", "consumer_vcs_repo",
                          "consumer_working_directory"]
RUN_TRIGGER_COLUMNS = ["upstream", "upstream_id", "downstream", "downstream_id"]


def _join(values) -> str:
    return ";".join(str(value) for value in values)


class InventoryExporter:
    """
    Reads the workspaces in scope and everything a migration plan depends on: team access,
    remote state consumers, run triggers, variables (keys only, never values) and variable sets.
    The tables are the source of truth a team reviews and plans waves from; inventory.json
    carries the same data for tools.
    """

    def __init__(self, args: MigratorArgs) -> None:
        self.args = args
        self.tfc = TFCClient(args.tfc_hostname, args.tfc_token)
        self.team_names: Dict[str, str] = {}
        self.agent_pool_names: Dict[str, Optional[str]] = {}
        self.team_access_readable = True
        self.warnings: List[str] = []

    def warn(self, message: str) -> None:
        ConsoleOutput.warning(message)
        self.warnings.append(message)

    def in_scope(self, workspace_name: str) -> bool:
        if self.args.workspace_names is not None and workspace_name.lower() not in self.args.workspace_names:
            return False
        return matches_workspace_patterns(workspace_name, self.args.workspaces)

    def run(self) -> int:
        organization = self.args.tfc_organization
        ConsoleOutput.section(f"Inventory of TFC/E organization '{organization}'")
        self.tfc.get_organization(organization)

        projects = {project["id"]: project["attributes"]["name"] for project in self.tfc.get_projects(organization)}
        project_id = None
        if self.args.tfc_project:
            project_id = next((pid for pid, name in projects.items() if name == self.args.tfc_project), None)
            if not project_id:
                raise errors.InvalidInputError(
                    f"Project '{self.args.tfc_project}' not found in organization '{organization}'"
                )

        self.load_team_names()

        workspaces: List[Dict] = []
        team_access: List[Dict] = []
        state_consumers: List[Dict] = []
        run_triggers: List[Dict] = []

        for tf_workspace, last_run in self.list_workspaces(project_id):
            name = tf_workspace["attributes"]["name"]
            ConsoleOutput.info(f"Reading workspace '{name}'...")
            project_name = projects.get(self._relationship_id(tf_workspace, "project"), "")

            row = self.workspace_row(tf_workspace, last_run, project_name)

            for access in self.workspace_team_access(tf_workspace):
                team_access.append({"scope": "workspace", "project": project_name, "workspace": name, **access})

            consumers = self.tfc.get_remote_state_consumers(tf_workspace)
            producer = {
                "producer": name,
                "producer_id": tf_workspace["id"],
                "producer_vcs_repo": row["vcs_repo"],
                "producer_working_directory": row["working_directory"],
                "sharing_mode": "global" if row["global_remote_state"] else "explicit",
            }
            if row["global_remote_state"]:
                state_consumers.append({**producer, "consumer": "", "consumer_id": ""})
            for consumer in consumers:
                consumer_attributes = consumer.get("attributes") or {}
                state_consumers.append({
                    **producer,
                    "consumer": consumer_attributes.get("name", ""),
                    "consumer_id": consumer["id"],
                    "consumer_vcs_repo": self._vcs_repo(consumer_attributes),
                    "consumer_working_directory": consumer_attributes.get("working-directory") or "",
                })
            row["remote_state_consumers"] = "*" if row["global_remote_state"] else len(consumers)

            triggers = self.tfc.get_run_triggers(tf_workspace["id"], "inbound")
            for trigger in triggers:
                attributes = trigger.get("attributes") or {}
                run_triggers.append({
                    "upstream": attributes.get("sourceable-name", ""),
                    "upstream_id": self._relationship_id(trigger, "sourceable"),
                    "downstream": name,
                    "downstream_id": tf_workspace["id"],
                })
            row["inbound_run_triggers"] = len(triggers)

            workspaces.append(row)

        project_ids = {project_id} if project_id else {
            pid for pid, name in projects.items() if name in {row["project"] for row in workspaces}
        }
        for pid in sorted(project_ids):
            for access in self.project_team_access(pid):
                team_access.append({"scope": "project", "project": projects.get(pid, pid), "workspace": "",
                                    **access})

        self.write_files(workspaces, team_access, state_consumers, run_triggers)
        return 0

    @staticmethod
    def _relationship_id(item: Dict, relationship: str) -> str:
        data = ((item.get("relationships") or {}).get(relationship) or {}).get("data") or {}
        return data.get("id", "") if isinstance(data, dict) else ""

    def list_workspaces(self, project_id: Optional[str]) -> List[Tuple[Dict, Optional[Dict]]]:
        """The workspaces in scope, each with its current run (included in the same listing)."""
        result: List[Tuple[Dict, Optional[Dict]]] = []
        seen_names: Set[str] = set()
        page = 1
        while True:
            response = self.tfc.get_workspaces(
                self.args.tfc_organization, page, project_id=project_id, include="current_run"
            )
            runs = {item["id"]: item for item in response.get("included") or [] if item.get("type") == "runs"}
            for tf_workspace in response["data"]:
                name = tf_workspace["attributes"]["name"]
                seen_names.add(name.lower())
                if self.in_scope(name):
                    result.append((tf_workspace, runs.get(self._relationship_id(tf_workspace, "current-run"))))
            page = response["meta"]["pagination"]["next-page"]
            if not page:
                break

        if self.args.workspace_names is not None:
            missing = sorted(self.args.workspace_names - seen_names)
            if missing:
                self.warn(
                    f"{len(missing)} workspace(s) listed in '{self.args.workspaces_file}' do not exist in TFC/E: "
                    f"{', '.join(missing)}"
                )
        return result

    def load_team_names(self) -> None:
        try:
            self.team_names = {
                team["id"]: team["attributes"]["name"] for team in self.tfc.get_teams(self.args.tfc_organization)
            }
        except errors.APIError as e:
            self.warn(f"Unable to list TFC/E teams, team access is reported by team id: {e}")

    def agent_pool_name(self, tf_workspace: Dict) -> str:
        agent_pool_id = self._relationship_id(tf_workspace, "agent-pool")
        if not agent_pool_id:
            return ""
        if agent_pool_id not in self.agent_pool_names:
            agent_pool = self.tfc.get_agent_pool(agent_pool_id)
            self.agent_pool_names[agent_pool_id] = (
                agent_pool["data"]["attributes"]["name"] if agent_pool and agent_pool.get("data") else agent_pool_id
            )
        return self.agent_pool_names[agent_pool_id]

    @staticmethod
    def _vcs_repo(attributes: Dict) -> str:
        return (attributes.get("vcs-repo") or {}).get("identifier") or attributes.get("vcs-repo-identifier") or ""

    def workspace_row(self, tf_workspace: Dict, last_run: Optional[Dict], project_name: str) -> Dict:
        attributes = tf_workspace["attributes"]
        vcs_repo = attributes.get("vcs-repo") or {}

        variables = self.tfc.get_workspace_vars(self.args.tfc_organization, attributes["name"]).get("data", [])
        keys = [variable["attributes"]["key"] for variable in variables]
        sensitive = [variable["attributes"]["key"] for variable in variables if variable["attributes"]["sensitive"]]
        credentials = [key for key in keys if key.upper().startswith(CREDENTIAL_PREFIXES)]

        try:
            variable_sets = [
                item["attributes"]["name"]
                for item in self.tfc.get_all(f"workspaces/{tf_workspace['id']}/varsets")
            ]
        except errors.APIError as e:
            self.warn(f"Unable to list the variable sets of '{attributes['name']}': {e}")
            variable_sets = []

        execution_mode = attributes.get("execution-mode")
        if not execution_mode:
            execution_mode = "remote" if attributes.get("operations", True) else "local"

        return {
            "name": attributes["name"],
            "id": tf_workspace["id"],
            "project": project_name,
            "vcs_repo": self._vcs_repo(attributes),
            "vcs_branch": vcs_repo.get("branch") or "",
            "working_directory": attributes.get("working-directory") or "",
            "terraform_version": attributes.get("terraform-version") or "",
            "execution_mode": execution_mode,
            "agent_pool": self.agent_pool_name(tf_workspace),
            "auto_apply": bool(attributes.get("auto-apply")),
            "global_remote_state": bool(attributes.get("global-remote-state")),
            "locked": bool(attributes.get("locked")),
            "resource_count": attributes.get("resource-count", ""),
            "has_state": bool(self._relationship_id(tf_workspace, "current-state-version")),
            "last_run_status": (last_run or {}).get("attributes", {}).get("status", ""),
            "last_run_at": (last_run or {}).get("attributes", {}).get("created-at", ""),
            "latest_change_at": attributes.get("latest-change-at") or "",
            "variable_count": len(keys),
            "sensitive_variables": _join(sensitive),
            "credential_variables": _join(credentials),
            "variable_sets": _join(variable_sets),
        }

    def _team_access_entry(self, item: Dict, permissions: Tuple[str, ...]) -> Dict:
        attributes = item.get("attributes") or {}
        team_id = self._relationship_id(item, "team")
        entry = {
            "team": self.team_names.get(team_id, team_id),
            "team_id": team_id,
            "access": attributes.get("access", ""),
        }
        for permission in permissions:
            value = attributes.get(permission)
            entry[permission] = "" if value is None else value
        return entry

    def workspace_team_access(self, tf_workspace: Dict) -> List[Dict]:
        if not self.team_access_readable:
            return []
        try:
            items = self.tfc.get_team_workspaces(tf_workspace["id"])
        except errors.APIError as e:
            # The token needs permission to manage team access; one warning is enough.
            self.team_access_readable = False
            self.warn(f"Unable to read TFC/E team access, the team-access table will be incomplete: {e}")
            return []
        return [self._team_access_entry(item, TEAM_WORKSPACE_PERMISSIONS) for item in items]

    def project_team_access(self, project_id: str) -> List[Dict]:
        if not self.team_access_readable:
            return []
        try:
            items = self.tfc.get_team_projects(project_id)
        except errors.APIError as e:
            self.warn(f"Unable to read the team access of project '{project_id}': {e}")
            return []
        return [self._team_access_entry(item, ()) for item in items]

    def team_map_example(self, team_access: List[Dict]) -> Dict:
        """A --team-map skeleton listing every team and access level found, for a person to fill in."""
        teams = sorted({row["team"] for row in team_access})
        workspace_levels = sorted({row["access"] for row in team_access if row["scope"] == "workspace"})
        project_levels = sorted({row["access"] for row in team_access if row["scope"] == "project"})
        return {
            "_comment": "Map every TFC/E team to an existing Scalr team, and every access level to Scalr role "
                        "names. Teams or levels left unmapped are reported and get no access policy.",
            "teams": {team: team for team in teams},
            "access_levels": {level: [] for level in workspace_levels},
            "project_access_levels": {level: [] for level in project_levels},
            "project_access": "ignore",
            "overrides": [],
        }

    def write_files(self, workspaces: List[Dict], team_access: List[Dict], state_consumers: List[Dict],
                    run_triggers: List[Dict]) -> None:
        directory = self.args.inventory_dir
        os.makedirs(directory, exist_ok=True)

        extension = self.args.inventory_format
        for name, columns, rows in (
                ("workspaces", WORKSPACE_COLUMNS, workspaces),
                ("team-access", TEAM_ACCESS_COLUMNS, team_access),
                ("state-consumers", STATE_CONSUMER_COLUMNS, state_consumers),
                ("run-triggers", RUN_TRIGGER_COLUMNS, run_triggers),
        ):
            write_table(os.path.join(directory, f"{name}.{extension}"), columns, rows)

        with open(os.path.join(directory, "team-map.example.json"), "w") as f:
            json.dump(self.team_map_example(team_access), f, indent=2)

        with open(os.path.join(directory, "inventory.json"), "w") as f:
            json.dump({
                "generated_at": utc_now(),
                "tfc_hostname": self.args.tfc_hostname,
                "organization": self.args.tfc_organization,
                "project": self.args.tfc_project,
                "workspaces": workspaces,
                "team_access": team_access,
                "state_consumers": state_consumers,
                "run_triggers": run_triggers,
                "warnings": self.warnings,
            }, f, indent=2)

        ConsoleOutput.section("Inventory Summary")
        ConsoleOutput.success(
            f"{len(workspaces)} workspace(s), {len(team_access)} team access grant(s), "
            f"{sum(1 for row in state_consumers if row['consumer_id'])} remote state consumer link(s), "
            f"{len(run_triggers)} run trigger(s)"
        )
        ConsoleOutput.info(f"Inventory written to {directory}/")
