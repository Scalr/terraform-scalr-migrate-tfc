"""Read-only TFC/E discovery: no-state workspaces and cross-workspace dependencies.

This does not touch Scalr or migrate anything - it only reads from TFC/E to help
plan a migration:
  - Workspaces with no current state (likely safe to skip migrating)
  - Cross-workspace dependencies via remote state consumers and run triggers,
    so you can see which workspaces are "hubs" (many dependents) before deciding
    on migration order.

The CLI entrypoint for this lives in tfc-discovery/discover.py; this module
holds the reusable logic and is kept inside the package so it can share
TFCClient, ConsoleOutput, and error handling with the migrator instead of
duplicating them.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

from scalr_tfc_migrate import errors
from scalr_tfc_migrate.clients import TFCClient
from scalr_tfc_migrate.console import ConsoleOutput


def workspace_has_state(workspace: Dict) -> bool:
    """True if the workspace has a current state version.

    Mirrors the check used during migration (see MigrationService.get_current_state):
    a workspace with no applies yet has no `links` on its current-state-version
    relationship.
    """
    current_state = (workspace.get("relationships") or {}).get("current-state-version") or {}
    return bool(current_state.get("links"))


def workspace_project_id(workspace: Dict) -> Optional[str]:
    project = (workspace.get("relationships") or {}).get("project") or {}
    return (project.get("data") or {}).get("id")


def workspace_resource_count(workspace: Dict) -> int:
    """Number of resources in the workspace's current state, straight from the
    workspace attribute TFC already returns in the workspace list response -
    no extra API call needed."""
    return (workspace.get("attributes") or {}).get("resource-count") or 0


def recommend(has_state: bool, dependent_names: List[str]) -> str:
    """Turn has_state + who-depends-on-this-workspace into an actual call to
    make, rather than leaving the reader to combine two raw columns themselves."""
    if not has_state:
        if dependent_names:
            plural = "workspace" if len(dependent_names) == 1 else "workspaces"
            return f"Review - no state, but {len(dependent_names)} {plural} depend(s) on it"
        return "Skip - no state, not referenced by other workspaces"
    if dependent_names:
        plural = "dependent" if len(dependent_names) == 1 else "dependents"
        return f"Migrate first - {len(dependent_names)} {plural}"
    return "Migrate"


def days_since(timestamp: str) -> int:
    run_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - run_time).days


class DiscoveryService:
    def __init__(self, tfc: TFCClient, organization: str, project_id: Optional[str] = None,
                 stale_days: Optional[int] = None):
        self.tfc = tfc
        self.organization = organization
        self.project_id = project_id
        # Opt-in: fetching each workspace's latest run is an extra API call per
        # workspace, on top of the remote-state-consumers/run-triggers calls
        # already made, so only pay for it when the caller actually wants the
        # staleness/"are we getting value" check.
        self.stale_days = stale_days

    @staticmethod
    def _paginate(fetch_page: Callable[[int], Dict]) -> List[Dict]:
        """Collect every `data` entry across all pages of a JSON:API list response."""
        items: List[Dict] = []
        page = 1
        while True:
            response = fetch_page(page)
            data = response.get("data", [])
            items.extend(data)
            pagination = (response.get("meta") or {}).get("pagination") or {}
            total_pages = pagination.get("total-pages", 1)
            if not data or page >= total_pages:
                break
            page += 1
        return items

    def _fetch_all_workspaces(self) -> List[Dict]:
        return self._paginate(
            lambda page: self.tfc.get_workspaces(self.organization, page=page, project_id=self.project_id)
        )

    def _fetch_project_names(self) -> Dict[str, str]:
        """Map project id -> project name, for the "TFC Project" CSV/report column."""
        try:
            projects = self._paginate(
                lambda page: self.tfc.list_projects(self.organization, page=page)
            )
        except errors.APIError as e:
            ConsoleOutput.warning(f"Could not fetch projects for '{self.organization}': {e}")
            return {}
        return {p["id"]: p["attributes"]["name"] for p in projects}

    def discover(self) -> Dict:
        ConsoleOutput.info(f"Fetching workspaces for '{self.organization}'...")
        workspaces = self._fetch_all_workspaces()
        project_names = self._fetch_project_names()
        ConsoleOutput.info(f"Found {len(workspaces)} workspace(s). Checking state and dependencies...")
        id_to_name = {ws["id"]: ws["attributes"]["name"] for ws in workspaces}

        no_state_workspaces = [
            {"id": ws["id"], "name": ws["attributes"]["name"]}
            for ws in workspaces
            if not workspace_has_state(ws)
        ]

        dependencies: List[Dict] = []
        seen_edges: Set[Tuple[str, str, str]] = set()
        depends_on_by_id: Dict[str, Set[str]] = {}  # dst_id -> set of src_id it depends on
        dependents_by_id: Dict[str, Set[str]] = {}  # src_id -> set of dst_id that depend on it

        def add_edge(src_id: Optional[str], dst_id: Optional[str], dep_type: str) -> None:
            if not src_id or not dst_id or src_id == dst_id:
                return
            key = (src_id, dst_id, dep_type)
            if key in seen_edges:
                return
            seen_edges.add(key)
            dependencies.append({
                "from": id_to_name.get(src_id, src_id),
                "from_id": src_id,
                "to": id_to_name.get(dst_id, dst_id),
                "to_id": dst_id,
                "type": dep_type,
            })
            depends_on_by_id.setdefault(dst_id, set()).add(src_id)
            dependents_by_id.setdefault(src_id, set()).add(dst_id)

        activity_by_id: Dict[str, Dict] = {}  # ws_id -> {"last_run_at": str|None, "days_since_last_run": int|None}

        for index, ws in enumerate(workspaces, start=1):
            ws_id = ws["id"]
            ws_name = ws["attributes"]["name"]
            print(f"  [{index}/{len(workspaces)}] {ws_name}")

            # Staleness check (--stale-days): only meaningful for workspaces
            # that actually have state - an empty workspace with no runs isn't
            # "paying for infrastructure nobody's touching", it's just unused.
            if self.stale_days is not None and workspace_has_state(ws):
                try:
                    latest_run = self.tfc.get_latest_run(ws_id)
                except errors.APIError as e:
                    ConsoleOutput.warning(f"Could not fetch runs for '{ws_name}': {e}")
                    latest_run = None
                if latest_run:
                    last_run_at = latest_run["attributes"]["created-at"]
                    activity_by_id[ws_id] = {
                        "last_run_at": last_run_at,
                        "days_since_last_run": days_since(last_run_at),
                    }
                else:
                    # Has state but no run history at all (e.g. state was pushed
                    # directly) - at least as notable as a merely old run.
                    activity_by_id[ws_id] = {"last_run_at": None, "days_since_last_run": None}

            # Remote state consumers: this workspace is the producer, so edges
            # point from here to each consumer. Iterating every workspace as the
            # producer covers the whole graph exactly once.
            try:
                consumers = self._paginate(
                    lambda page, ws_id=ws_id: self.tfc.get_remote_state_consumers(ws_id, page=page)
                )
            except errors.APIError as e:
                ConsoleOutput.warning(f"Could not fetch remote state consumers for '{ws_name}': {e}")
                consumers = []
            for consumer in consumers:
                add_edge(ws_id, consumer.get("id"), "remote_state_consumer")

            # Run triggers: fetch inbound (this workspace is triggered by others).
            # Doing this from the downstream side, for every workspace, also
            # covers the whole graph exactly once without needing the outbound
            # direction too.
            try:
                triggers = self._paginate(
                    lambda page, ws_id=ws_id: self.tfc.get_run_triggers(ws_id, trigger_type="inbound", page=page)
                )
            except errors.APIError as e:
                ConsoleOutput.warning(f"Could not fetch run triggers for '{ws_name}': {e}")
                triggers = []
            for trigger in triggers:
                sourceable = (trigger.get("relationships") or {}).get("sourceable") or {}
                source_id = (sourceable.get("data") or {}).get("id")
                add_edge(source_id, ws_id, "run_trigger")

        dependents_count: Dict[str, int] = {}
        for edge in dependencies:
            dependents_count[edge["from"]] = dependents_count.get(edge["from"], 0) + 1
        hubs = [
            {"name": name, "dependent_count": count}
            for name, count in sorted(dependents_count.items(), key=lambda kv: kv[1], reverse=True)
        ]

        # One row per workspace, for the CSV export and for anyone consuming the
        # JSON report who wants a flat table instead of separate lists.
        workspace_rows = []
        for ws in workspaces:
            ws_id = ws["id"]
            has_state = workspace_has_state(ws)
            depends_on_names = sorted(
                id_to_name.get(src_id, src_id) for src_id in depends_on_by_id.get(ws_id, set())
            )
            dependent_names = sorted(
                id_to_name.get(dst_id, dst_id) for dst_id in dependents_by_id.get(ws_id, set())
            )
            activity = activity_by_id.get(ws_id)
            days_inactive = activity["days_since_last_run"] if activity else None
            # None (not True/False) when this row was never checked at all - either
            # --stale-days wasn't passed, or this workspace has no state to check.
            # Distinct from "checked and confirmed not stale" (False).
            is_stale = None
            if activity is not None:
                is_stale = days_inactive is None or days_inactive >= self.stale_days
            workspace_rows.append({
                "project": project_names.get(workspace_project_id(ws), ""),
                "name": ws["attributes"]["name"],
                "id": ws_id,
                "has_state": has_state,
                "resource_count": workspace_resource_count(ws),
                "depends_on": depends_on_names,
                "dependents": dependent_names,
                "recommendation": recommend(has_state, dependent_names),
                "last_run_at": activity["last_run_at"] if activity else None,
                "days_since_last_run": days_inactive,
                "stale": is_stale,
            })
        workspace_rows.sort(key=lambda row: (row["project"], row["name"]))

        stale_workspaces = [row for row in workspace_rows if row["stale"]] if self.stale_days is not None else []
        total_resources = sum(row["resource_count"] for row in workspace_rows)

        return {
            "organization": self.organization,
            "total_workspaces": len(workspaces),
            "total_resources": total_resources,
            "no_state_workspaces": no_state_workspaces,
            "dependencies": dependencies,
            "hubs": hubs,
            "workspaces": workspace_rows,
            "stale_days": self.stale_days,
            "stale_workspaces": stale_workspaces,
        }


def print_report(report: Dict) -> None:
    ConsoleOutput.section(f"TFC Discovery: {report['organization']}")
    ConsoleOutput.info(f"Total workspaces: {report['total_workspaces']}")
    ConsoleOutput.info(f"Total resources under management: {report['total_resources']}")

    no_state = report["no_state_workspaces"]
    ConsoleOutput.section(f"Workspaces with no state ({len(no_state)})")
    if no_state:
        for ws in no_state:
            print(f"  - {ws['name']} ({ws['id']})")
        ConsoleOutput.info("These have no resources under management and likely don't need to be migrated.")
    else:
        ConsoleOutput.info("None found - every workspace has state.")

    dependencies = report["dependencies"]
    ConsoleOutput.section(f"Cross-workspace dependencies ({len(dependencies)})")
    if dependencies:
        by_source: Dict[str, List[Dict]] = {}
        for edge in dependencies:
            by_source.setdefault(edge["from"], []).append(edge)

        # Grouped by source and sorted by dependent count (most first), so
        # hubs and their dependents read as one block instead of a flat
        # edge list you have to cross-reference against a separate ranking.
        for source in sorted(by_source, key=lambda name: (-len(by_source[name]), name)):
            edges = by_source[source]
            plural = "dependent" if len(edges) == 1 else "dependents"
            print(f"  {source}  ({len(edges)} {plural})")
            for edge in sorted(edges, key=lambda e: e["to"]):
                label = "remote state" if edge["type"] == "remote_state_consumer" else "run trigger"
                print(f"    -> {edge['to']}  [{label}]")
    else:
        ConsoleOutput.info("None found.")

    if report.get("stale_days") is not None:
        stale = report["stale_workspaces"]
        ConsoleOutput.section(f"Stale workspaces with state, no runs in {report['stale_days']}+ days ({len(stale)})")
        if stale:
            for ws in sorted(stale, key=lambda row: (row["days_since_last_run"] is not None, row["days_since_last_run"]), reverse=True):
                resources = f"{ws['resource_count']} resource(s)"
                if ws["days_since_last_run"] is None:
                    print(f"  - {ws['name']}  ({resources}, never run)")
                else:
                    print(f"  - {ws['name']}  ({resources}, last run {ws['days_since_last_run']} days ago)")
            stale_resources = sum(ws["resource_count"] for ws in stale)
            ConsoleOutput.info(
                f"{stale_resources} resource(s) across these workspaces haven't been touched recently - worth "
                "checking whether they're still needed, since they're being paid for either way."
            )
        else:
            ConsoleOutput.info("None found - every workspace with state has run recently.")


def write_csv(report: Dict, path: str) -> None:
    """Write one row per workspace: TFC project, workspace name, whether state
    exists, what it depends on and what depends on it (via remote state
    consumption or a run trigger, semicolon-joined if more than one), a
    sortable dependent count, and a computed Recommendation so the sheet is
    something you can act on directly instead of raw data to cross-reference
    by hand.

    Last Run / Days Since Last Run / Stale are populated only when --stale-days
    was used (blank otherwise); the columns are always present so the CSV
    schema doesn't change shape depending on which flags were passed."""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "TFC Project", "Workspace", "Has State", "Resource Count", "Depends On", "Dependents",
            "Dependent Count", "Recommendation", "Last Run", "Days Since Last Run", "Stale",
        ])
        for ws in report["workspaces"]:
            days_inactive = ws.get("days_since_last_run")
            stale = ws.get("stale")
            writer.writerow([
                ws["project"],
                ws["name"],
                "Yes" if ws["has_state"] else "No",
                ws["resource_count"],
                "; ".join(ws["depends_on"]),
                "; ".join(ws["dependents"]),
                len(ws["dependents"]),
                ws["recommendation"],
                ws.get("last_run_at") or "",
                days_inactive if days_inactive is not None else "",
                "" if stale is None else ("Yes" if stale else "No"),
            ])


def _read_tfrc_token(hostname: Optional[str]) -> Optional[str]:
    if not hostname:
        return None
    path = os.path.expanduser("~/.terraform.d/credentials.tfrc.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return (data.get("credentials", {}).get(hostname, {}) or {}).get("token")
    except (OSError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover TFC/E workspaces with no state and cross-workspace dependencies "
                     "(remote state consumers, run triggers) to help plan a migration."
    )
    parser.add_argument("--tfc-hostname", type=str, default=os.environ.get("TFC_HOSTNAME", "app.terraform.io"),
                        help="TFC/E hostname (default: app.terraform.io)")
    parser.add_argument("--tfc-token", type=str, default=os.environ.get("TFC_TOKEN"), help="TFC/E API token")
    parser.add_argument("--tfc-organization", type=str, default=os.environ.get("TFC_ORGANIZATION"),
                        help="TFC/E organization name")
    parser.add_argument("--tfc-project", type=str, default=os.environ.get("TFC_PROJECT"),
                        help="Optional TFC project name to limit discovery to")
    parser.add_argument("--json", type=str, help="Optional path to also write the full report as JSON")
    parser.add_argument("--csv", type=str,
                        help="Optional path to write a CSV (TFC Project, Workspace, Has State, Depends On), "
                             "one row per workspace")
    parser.add_argument("--stale-days", type=int, default=None,
                        help="Optional: flag workspaces that have state but no run in this many days or more "
                             "(costs one extra API call per workspace with state; not checked unless passed)")
    args = parser.parse_args()

    if not args.tfc_token:
        args.tfc_token = _read_tfrc_token(args.tfc_hostname)

    missing = [name for name, value in (("tfc-organization", args.tfc_organization), ("tfc-token", args.tfc_token))
               if not value]
    if missing:
        ConsoleOutput.error(
            f"Missing required argument(s): {', '.join(missing)}. "
            "Pass them as flags, set TFC_TOKEN/TFC_ORGANIZATION, or run `terraform login` first."
        )
        sys.exit(1)

    try:
        tfc = TFCClient(args.tfc_hostname, args.tfc_token)

        project_id = None
        if args.tfc_project:
            project = tfc.get_project(args.tfc_organization, args.tfc_project)
            if not project:
                ConsoleOutput.error(
                    f"Project '{args.tfc_project}' not found in organization '{args.tfc_organization}'"
                )
                sys.exit(1)
            project_id = project["id"]

        service = DiscoveryService(tfc, args.tfc_organization, project_id, stale_days=args.stale_days)
        report = service.discover()
    except errors.NetworkError as e:
        ConsoleOutput.error(f"Discovery failed: {e}")
        sys.exit(1)
    except errors.APIError as e:
        ConsoleOutput.error(f"Discovery failed: {e}")
        sys.exit(1)

    print_report(report)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2)
        ConsoleOutput.success(f"Full report written to {args.json}")

    if args.csv:
        write_csv(report, args.csv)
        ConsoleOutput.success(f"CSV written to {args.csv}")


if __name__ == "__main__":
    main()
