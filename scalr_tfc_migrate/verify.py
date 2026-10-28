"""--verify: compare workspaces migrated by earlier runs with their TFC/E source."""
import fnmatch
import time
import traceback
from typing import Dict, List, Optional

from scalr_tfc_migrate import errors, report as run_report
from scalr_tfc_migrate.console import ConsoleOutput

# Run statuses after which a plan-only run makes no more progress on its own.
FINAL_RUN_STATUSES = {"planned_and_finished", "errored", "canceled", "discarded", "policy_override", "applied"}
PLAN_TIMEOUT_SECONDS = 30 * 60
PLAN_POLL_SECONDS = 10


class MigrationVerifier:
    """
    Reads both sides of every workspace in the workspace map that matches the run's scope and
    reports what does not line up. Read-only, except --verify-plan, which queues plan-only runs.
    """

    def __init__(self, service) -> None:
        self.service = service
        self.args = service.args
        self.report = service.report
        self.tfc = service.tfc
        self.scalr = service.scalr

    def run(self) -> int:
        ConsoleOutput.section("Verifying migrated workspaces")
        entries = [
            entry for entry in self.service.workspace_map.entries()
            if entry["tfc"]["hostname"] == self.args.tfc_hostname
            and entry["tfc"]["organization"] == self.args.tfc_organization
            and entry["scalr"]["environment"] == self.args.scalr_environment
            and self.service.should_migrate_workspace(entry["tfc"]["workspace"])
        ]
        if not entries:
            message = (
                f"No workspace of '{self.args.tfc_organization}' migrated into '{self.args.scalr_environment}' "
                f"matches the scope in {self.service.workspace_map.path}"
            )
            ConsoleOutput.warning(message)
            self.report.warnings.append(message)
            return self.finish()

        self.report.details["run_triggers"] = "not verified: the Scalr API cannot list run triggers"
        for map_entry in sorted(entries, key=lambda item: item["tfc"]["workspace"]):
            self.verify_workspace(map_entry)

        if self.args.verify_plan:
            self.verify_plans()

        return self.finish()

    @staticmethod
    def check(entry: Dict, name: str, ok: bool, detail: Optional[str] = None) -> None:
        entry.setdefault("checks", []).append({"check": name, "ok": ok, "detail": detail})
        if not ok and entry["status"] != run_report.STATUS_FAILED:
            entry["status"] = run_report.STATUS_MISMATCH

    def verify_workspace(self, map_entry: Dict) -> None:
        name = map_entry["tfc"]["workspace"]
        scalr_id = map_entry["scalr"]["workspace_id"]
        ConsoleOutput.info(f"Verifying workspace '{name}'...")
        entry = self.report.begin_workspace(name, map_entry["tfc"]["id"])
        entry.update({"status": run_report.STATUS_VERIFIED, "scalr_id": scalr_id})
        try:
            scalr_workspace = self.scalr.get_workspace_by_id(scalr_id)
            self.check(entry, "scalr-workspace", bool(scalr_workspace),
                       None if scalr_workspace else f"Scalr workspace {scalr_id} does not exist")
            if not scalr_workspace:
                return
            entry["scalr_workspace"] = scalr_workspace

            tf_workspace = self.tfc.get_workspace(map_entry["tfc"]["id"])
            self.service.cache_tfc_workspace(tf_workspace)

            self.verify_state(tf_workspace, scalr_id, entry)
            if self.args.lock:
                locked = bool(tf_workspace["attributes"].get("locked"))
                self.check(entry, "tfc-locked", locked, None if locked else "TFC/E workspace is not locked")
            self.verify_variables(tf_workspace, scalr_id, entry)
            self.verify_state_consumers(tf_workspace, scalr_id, entry)
            if self.service.team_map:
                self.verify_access_policies(tf_workspace, scalr_id, entry)
        except Exception as e:
            entry["status"] = run_report.STATUS_FAILED
            entry["error"] = str(e)
            ConsoleOutput.error(f"Unable to verify workspace '{name}': {e}")
            if self.args.debug_enabled:
                ConsoleOutput.debug(traceback.format_exc())
        finally:
            self.report.end_workspace()

    def verify_state(self, tf_workspace: Dict, scalr_id: str, entry: Dict) -> None:
        tfc_state = self.tfc.get_current_state(tf_workspace)
        scalr_state = self.scalr.get_current_state(scalr_id)
        tfc_serial = tfc_state["attributes"].get("serial") if tfc_state else None
        scalr_serial = scalr_state["data"]["attributes"]["serial"] if scalr_state else None
        entry["state"] = {"tfc_serial": tfc_serial, "scalr_serial": scalr_serial}

        if tfc_serial is None:
            self.check(entry, "state", True, "TFC/E workspace has no state")
        elif scalr_serial is None:
            self.check(entry, "state", False, "Scalr workspace has no state")
        elif scalr_serial < tfc_serial:
            # TFC/E state moved on after the migration: someone applied there.
            self.check(entry, "state", False,
                       f"TFC/E state serial {tfc_serial} is newer than Scalr's {scalr_serial}")
        else:
            self.check(entry, "state", True)

    def verify_variables(self, tf_workspace: Dict, scalr_id: str, entry: Dict) -> None:
        if self.args.skip_variables == "*":
            return
        service = self.service
        skip_patterns = self.args.skip_variables.split(',') if self.args.skip_variables else []
        expected = set()
        for api_var in service.get_workspace_vars_cached(tf_workspace):
            key = api_var["attributes"]["key"]
            if any(fnmatch.fnmatch(key, pattern.strip()) for pattern in skip_patterns):
                continue
            if service.is_provider_credential(api_var):
                continue
            expected.add((key, service.normalize_variable_category(api_var["attributes"]["category"])))

        actual = {
            (variable["attributes"]["key"], service.normalize_variable_category(variable["attributes"]["category"]))
            for variable in self.scalr.get_workspace_variables(scalr_id)
        }
        missing = sorted(f"{key} ({category})" for key, category in expected - actual)
        self.check(entry, "variables", not missing, f"missing in Scalr: {', '.join(missing)}" if missing else None)

    def verify_state_consumers(self, tf_workspace: Dict, scalr_id: str, entry: Dict) -> None:
        expected = {}
        for consumer in self.tfc.get_remote_state_consumers(tf_workspace):
            mapped = self.service.workspace_map.get_by_tfc_id(consumer["id"])
            if mapped:
                expected[mapped["scalr"]["workspace_id"]] = mapped["scalr"]["workspace"]
        if not expected:
            return

        try:
            actual = {
                item["id"] for item in
                self.scalr.get(f"workspaces/{scalr_id}/relationships/remote-state-consumers").get("data") or []
            }
        except errors.APIError as e:
            self.check(entry, "remote-state-consumers", True, f"not verified: {e}")
            return
        missing = sorted(name for consumer_id, name in expected.items() if consumer_id not in actual)
        self.check(entry, "remote-state-consumers", not missing,
                   f"not allowed to read the state in Scalr: {', '.join(missing)}" if missing else None)

    def verify_access_policies(self, tf_workspace: Dict, scalr_id: str, entry: Dict) -> None:
        service = self.service
        missing = []
        for planned in service.resolve_team_access(tf_workspace):
            if planned["problem"]:
                # Unmapped on purpose or by omission; the migration reported it, there is nothing to compare.
                continue
            team_id = service.find_scalr_team_id(planned["team"])
            if not team_id or not self.scalr.list_access_policies(
                    {"filter[team]": team_id, "filter[workspace]": scalr_id}):
                missing.append(planned["team"])
        self.check(entry, "access-policies", not missing,
                   f"no access policy for team(s): {', '.join(missing)}" if missing else None)

    def verify_plans(self) -> None:
        """Queue a plan-only run in every verified workspace and wait for all of them."""
        ConsoleOutput.section("Queueing plan-only runs")
        pending: Dict[str, Dict] = {}
        for entry in self.report.workspaces.values():
            scalr_workspace = entry.pop("scalr_workspace", None)
            if not scalr_workspace:
                continue
            if not (scalr_workspace.get("attributes") or {}).get("vcs-repo"):
                self.check(entry, "plan", True,
                           "not verified: CLI-driven workspace, Scalr has no configuration to plan with")
                continue
            try:
                run = self.scalr.create_plan_only_run(
                    entry["scalr_id"], "Post-migration check by the TFC/E → Scalr migrator"
                )["data"]
            except errors.APIError as e:
                self.check(entry, "plan", False, f"unable to queue a plan: {e}")
                continue
            entry["plan_run_id"] = run["id"]
            pending[run["id"]] = entry

        deadline = time.time() + PLAN_TIMEOUT_SECONDS
        while pending and time.time() < deadline:
            ConsoleOutput.info(f"Waiting for {len(pending)} plan(s)...")
            time.sleep(PLAN_POLL_SECONDS)
            for run_id in list(pending):
                run = self.scalr.get_run(run_id)
                status = run["attributes"]["status"]
                if status not in FINAL_RUN_STATUSES:
                    continue
                entry = pending.pop(run_id)
                has_changes = run["attributes"].get("has-changes")
                entry["plan_has_changes"] = has_changes
                if status == "errored":
                    self.check(entry, "plan", False, f"plan errored: {run['attributes'].get('error-message')}")
                else:
                    self.check(entry, "plan", not has_changes,
                               f"plan {run_id} proposes changes" if has_changes else None)

        for run_id, entry in pending.items():
            self.check(entry, "plan", False, f"plan {run_id} did not finish within {PLAN_TIMEOUT_SECONDS // 60} minutes")

    def finish(self) -> int:
        for entry in self.report.workspaces.values():
            entry.pop("scalr_workspace", None)
        path = self.report.write(self.service.resource_manager.output_dir)

        ConsoleOutput.section("Verification Summary")
        summary = self.report.summary()
        if summary:
            ConsoleOutput.info(", ".join(f"{count} {status}" for status, count in summary.items()))
        problems: List[str] = []
        for entry in self.report.workspaces.values():
            for check in entry.get("checks", []):
                if not check["ok"]:
                    problems.append(f"{entry['name']}: {check['check']}: {check['detail']}")
            if entry["error"]:
                problems.append(f"{entry['name']}: {entry['error']}")
        for problem in problems:
            ConsoleOutput.warning(problem)
        ConsoleOutput.info(f"Verification report written to {path}")

        return 1 if problems else 0
