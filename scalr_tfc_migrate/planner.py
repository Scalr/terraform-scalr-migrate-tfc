"""--dry-run: what a migration with the same arguments would do, without changing anything."""
import fnmatch
import shutil
import traceback
from typing import Dict, List, Optional, Set

from scalr_tfc_migrate import errors, report as run_report
from scalr_tfc_migrate.console import ConsoleOutput


class MigrationPlanner:
    """
    Walks the same scope as a migration and asks TFC/E and Scalr read-only questions: what exists,
    what would be created, and what would stop a workspace from migrating. It never calls a
    method that writes, so it does not create the environment, lock workspaces, start TFC runs,
    update the workspace map or touch the generated code.
    """

    def __init__(self, service) -> None:
        self.service = service
        self.args = service.args
        self.report = service.report
        self.tfc = service.tfc
        self.scalr = service.scalr
        self.scope_ids: Set[str] = set()

    def run(self) -> int:
        try:
            self.plan()
        except Exception as e:
            # Still write what was planned so far: the blocker is the answer the caller needs.
            self.report.block(f"Planning stopped: {e}")
            if self.args.debug_enabled:
                ConsoleOutput.debug(traceback.format_exc())
        return self.finish()

    def plan(self) -> None:
        service = self.service
        ConsoleOutput.section("Planning migration (dry run: nothing is changed)")
        service.load_tofu()
        self.tfc.get_organization(self.args.tfc_organization)

        project_id = service.get_project_id()
        if self.args.tfc_project and not project_id:
            self.report.block(f"TFC/E project '{self.args.tfc_project}' does not exist")

        env = self.plan_environments()
        self.check_shared_dependencies()

        seen_names: Set[str] = set()
        tf_workspaces: List[Dict] = []
        next_page = 1
        while True:
            response = self.tfc.get_workspaces(self.args.tfc_organization, next_page, project_id=project_id)
            for tf_workspace in response["data"]:
                service.cache_tfc_workspace(tf_workspace)
                name = tf_workspace["attributes"]["name"]
                seen_names.add(name)
                if service.should_migrate_workspace(name):
                    tf_workspaces.append(tf_workspace)
                    self.scope_ids.add(tf_workspace["id"])
            next_page = response["meta"]["pagination"]["next-page"]
            if not next_page:
                break
        service.warn_unlisted_workspaces(seen_names)

        if self.args.migrate_variable_sets_only:
            ConsoleOutput.info(f"{len(tf_workspaces)} workspace(s) in scope for variable sets only")
        else:
            for tf_workspace in tf_workspaces:
                self.plan_workspace(tf_workspace, env)
            if not self.args.skip_run_triggers:
                self.plan_run_triggers()

        self.plan_variable_sets(project_id)

    def will_exist(self, tfc_workspace_id: str) -> bool:
        """In Scalr after this run: migrated by it, or by an earlier one."""
        return tfc_workspace_id in self.scope_ids or bool(self.service.workspace_map.get_by_tfc_id(tfc_workspace_id))

    def plan_environments(self) -> Optional[Dict]:
        env = self.scalr.get_environment(self.args.scalr_environment)
        self.report.details["environment"] = {
            "name": self.args.scalr_environment,
            "id": env["id"] if env else None,
            "action": "reuse" if env else "create",
        }

        if not self.args.skip_post_migration:
            management_env = self.scalr.get_environment(self.args.management_env_name)
            management_workspace = self.scalr.get_workspace(
                management_env["id"], self.args.management_workspace_name
            ) if management_env else None
            self.report.details["management"] = {
                "environment": self.args.management_env_name,
                "workspace": self.args.management_workspace_name,
                "action": "reuse" if management_workspace else "create",
            }
        return env

    def check_shared_dependencies(self) -> None:
        """Arguments naming Scalr objects that do not exist stop every workspace, not just one."""
        service = self.service
        checks = []
        if self.args.vcs_name:
            checks.append(service.get_vcs_provider_id)
        if self.args.pc_name:
            checks.append(service.get_provider_configuration)
        if self.args.agent_pool_name:
            checks.append(service.get_agent_pool_id)
        for check in checks:
            try:
                check()
            except errors.MissingDataError as e:
                self.report.block(str(e))

        for ssh_key_id in service.workspace_defaults.ssh_key_ids():
            if not self.scalr.get_ssh_key(ssh_key_id):
                self.report.block(f"SSH key '{ssh_key_id}' from --workspace-defaults does not exist in Scalr")

    def plan_workspace(self, tf_workspace: Dict, env: Optional[Dict]) -> None:
        name = tf_workspace["attributes"]["name"]
        ConsoleOutput.info(f"Planning workspace '{name}'...")
        entry = self.report.begin_workspace(name, tf_workspace["id"])
        entry["status"] = run_report.STATUS_PLANNED
        try:
            scalr_workspace = self.scalr.get_workspace(env["id"], name) if env else None
            entry["action"] = "reuse" if scalr_workspace else "create"
            entry["scalr_id"] = scalr_workspace["id"] if scalr_workspace else None

            if not scalr_workspace:
                self.plan_workspace_settings(tf_workspace, entry)
            self.plan_state(tf_workspace, scalr_workspace, entry)
            self.plan_variables(tf_workspace, entry)
            if self.service.team_map:
                self.plan_team_access(tf_workspace, scalr_workspace, entry)

            locked = tf_workspace["attributes"].get("locked")
            entry["tfc_locked"] = "will-lock" if self.args.lock and not locked else bool(locked)

            consumers = self.tfc.get_remote_state_consumers(tf_workspace)
            entry["remote_state_consumers"] = [
                {"name": (consumer.get("attributes") or {}).get("name", consumer["id"]),
                 "action": "set" if self.will_exist(consumer["id"]) else "pending"}
                for consumer in consumers
            ]
        except Exception as e:
            entry["status"] = run_report.STATUS_FAILED
            entry["error"] = str(e)
            ConsoleOutput.error(f"Unable to plan workspace '{name}': {e}")
            if self.args.debug_enabled:
                ConsoleOutput.debug(traceback.format_exc())
        finally:
            self.report.end_workspace()

    def plan_workspace_settings(self, tf_workspace: Dict, entry: Dict) -> None:
        service = self.service
        attributes = tf_workspace["attributes"]
        name = attributes["name"]

        entry["terraform_version"] = service.enforce_max_version(
            attributes.get("terraform-version") or "1.6.0", "Workspace"
        )
        entry["workspace_defaults"] = service.workspace_defaults.for_workspace(name)

        if attributes.get("vcs-repo") and not self.args.vcs_name:
            message = "VCS-driven workspace, --vcs-name is required"
            self.report.warn(message, entry)
            self.report.block(f"{name}: {message}")

        pc_name = service.resolve_pc_name(name)
        if pc_name:
            try:
                entry["provider_configuration"] = pc_name if service.get_provider_configuration(pc_name) else None
            except errors.MissingDataError:
                entry["provider_configuration"] = None
            if not entry["provider_configuration"]:
                self.report.warn(f"Provider configuration '{pc_name}' does not exist in Scalr", entry)

        tfc_agent_pool = service.get_tfc_agent_pool_name(tf_workspace)
        if tfc_agent_pool:
            if service.find_scalr_agent_pool_by_name(tfc_agent_pool):
                entry["agent_pool"] = tfc_agent_pool
            elif self.args.agent_pool_name:
                entry["agent_pool"] = self.args.agent_pool_name
            else:
                self.report.warn(
                    f"TFC/E agent pool '{tfc_agent_pool}' has no Scalr counterpart with active agents, and "
                    f"--agent-pool-name is not set: runs would use Scalr's shared runners", entry
                )

    def plan_state(self, tf_workspace: Dict, scalr_workspace: Optional[Dict], entry: Dict) -> None:
        state = {"action": "unavailable", "tfc_serial": None, "scalr_serial": None}
        entry["state"] = state
        tfc_state = self.tfc.get_current_state(tf_workspace)
        if not tfc_state:
            self.report.warn("TFC/E workspace has no state file", entry)
            return
        state["tfc_serial"] = tfc_state["attributes"].get("serial")

        if scalr_workspace:
            scalr_state = self.scalr.get_current_state(scalr_workspace["id"])
            if scalr_state:
                state["scalr_serial"] = scalr_state["data"]["attributes"]["serial"]
        state["action"] = "up-to-date" if state["scalr_serial"] == state["tfc_serial"] else "upload"

    def plan_variables(self, tf_workspace: Dict, entry: Dict) -> None:
        if self.args.skip_variables == "*":
            return
        service = self.service
        skip_patterns = self.args.skip_variables.split(',') if self.args.skip_variables else []
        needs_tfc_run = False

        for api_var in service.get_workspace_vars_cached(tf_workspace):
            attributes = api_var["attributes"]
            key = attributes["key"]
            category = service.normalize_variable_category(attributes["category"])
            sensitive = attributes["sensitive"]

            pc_name = service.is_provider_credential(api_var)
            if any(fnmatch.fnmatch(key, pattern.strip()) for pattern in skip_patterns):
                action, reason = "skip", "skip-pattern"
            elif pc_name:
                action, reason = "skip", f"provider-credential:{pc_name}"
            elif sensitive and category == "shell":
                action, reason = "recover", "sensitive: read by a TFC/E run started in the workspace"
                needs_tfc_run = True
            elif sensitive:
                action, reason = "recover", "sensitive: read from the latest plan file"
            else:
                action, reason = "create", None
            self.report.add_variable(key, category, sensitive, action, reason)

        if needs_tfc_run and not shutil.which("terraform"):
            self.report.warn(
                "Sensitive shell variables are recovered through a TFC/E run started with the local "
                "`terraform` binary, which is not installed", entry
            )

    def plan_team_access(self, tf_workspace: Dict, scalr_workspace: Optional[Dict], entry: Dict) -> None:
        service = self.service
        try:
            planned_policies = service.resolve_team_access(tf_workspace)
        except errors.APIError as e:
            self.report.warn(f"Unable to read the TFC/E team access: {e}", entry)
            return

        for planned in planned_policies:
            result = {"team": planned["team"], "tfc_team": planned["tfc_team"], "access": planned["access"],
                      "roles": planned["roles"], "scope": "workspaces", "action": "create", "id": None}
            entry["access_policies"].append(result)

            if planned["problem"]:
                result["action"] = "unmapped"
                self.report.warn(planned["problem"], entry)
                continue

            team_id = service.find_scalr_team_id(planned["team"])
            missing = ([f"team '{planned['team']}'"] if not team_id else []) + [
                f"role '{role}'" for role in planned["roles"] if not service.find_scalr_role_id(role)
            ]
            if missing:
                result["action"] = "missing-in-scalr"
                self.report.warn(f"{', '.join(missing)} not found in Scalr", entry)
                continue

            if scalr_workspace and self.scalr.list_access_policies(
                    {"filter[team]": team_id, "filter[workspace]": scalr_workspace["id"]}):
                result["action"] = "exists"

    def plan_run_triggers(self) -> None:
        try:
            pairs = self.service.collect_run_triggers(sorted(self.scope_ids))
        except errors.APIError as e:
            self.report.warnings.append(f"Unable to read TFC/E run triggers: {e}")
            return

        for (upstream_id, downstream_id), (upstream_name, downstream_name) in sorted(
                pairs.items(), key=lambda item: item[1]):
            result = {
                "upstream": upstream_name,
                "downstream": downstream_name,
                "action": "create" if self.will_exist(upstream_id) and self.will_exist(downstream_id) else "pending",
            }
            for name in (upstream_name, downstream_name):
                if name in self.report.workspaces:
                    self.report.workspaces[name]["run_triggers"].append(result)

    def plan_variable_sets(self, project_id: Optional[str]) -> None:
        service = self.service
        if self.args.skip_variable_sets or self.args.skip_variables == "*":
            return
        if not self.scope_ids and not project_id:
            return

        for tf_var_set in service.list_all_variable_sets():
            name = tf_var_set["attributes"]["name"]
            if name == self.args.credentials_set_name:
                continue
            resolved = service._tfc_var_set_with_relationships(tf_var_set)
            if not service.should_include_tfc_variable_set(resolved, project_id, self.scope_ids):
                continue

            entry = self.report.variable_set(name, tf_var_set["id"])
            entry["shared"] = tf_var_set["attributes"].get("global", False)
            variables = service.list_all_variable_set_vars_safe(tf_var_set["id"])
            holders = [service.is_provider_credential(variable) for variable in variables]
            if variables and all(holders):
                entry["action"] = "skip"
                entry["reason"] = f"provider-credential:{holders[0]}"
                continue

            entry["action"] = "update" if self.scalr.get_var_sets(name=name)["data"] else "create"
            entry["variables"] = len(variables)
            entry["sensitive_variables"] = sorted(
                variable["attributes"]["key"] for variable in variables if variable["attributes"]["sensitive"]
            )

    def finish(self) -> int:
        path = self.report.write(self.service.resource_manager.output_dir)

        ConsoleOutput.section("Dry Run Summary")
        actions: Dict[str, int] = {}
        for entry in self.report.workspaces.values():
            action = entry.get("action") or entry["status"]
            actions[action] = actions.get(action, 0) + 1
        if actions:
            ConsoleOutput.info("Workspaces: " + ", ".join(f"{count} to {action}" for action, count in actions.items()))
        if self.report.variable_sets:
            ConsoleOutput.info(f"Variable sets in scope: {len(self.report.variable_sets)}")
        for blocker in self.report.blockers:
            ConsoleOutput.error(f"Blocker: {blocker}")
        ConsoleOutput.info(f"Plan written to {path}")

        return 1 if self.report.blockers or self.report.failed() else 0
