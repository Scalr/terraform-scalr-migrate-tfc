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
import json
import os
import sys
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


class DiscoveryService:
    def __init__(self, tfc: TFCClient, organization: str, project_id: Optional[str] = None):
        self.tfc = tfc
        self.organization = organization
        self.project_id = project_id

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

    def discover(self) -> Dict:
        ConsoleOutput.info(f"Fetching workspaces for '{self.organization}'...")
        workspaces = self._fetch_all_workspaces()
        ConsoleOutput.info(f"Found {len(workspaces)} workspace(s). Checking state and dependencies...")
        id_to_name = {ws["id"]: ws["attributes"]["name"] for ws in workspaces}

        no_state_workspaces = [
            {"id": ws["id"], "name": ws["attributes"]["name"]}
            for ws in workspaces
            if not workspace_has_state(ws)
        ]

        dependencies: List[Dict] = []
        seen_edges: Set[Tuple[str, str, str]] = set()

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

        for index, ws in enumerate(workspaces, start=1):
            ws_id = ws["id"]
            ws_name = ws["attributes"]["name"]
            print(f"  [{index}/{len(workspaces)}] {ws_name}")

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

        return {
            "organization": self.organization,
            "total_workspaces": len(workspaces),
            "no_state_workspaces": no_state_workspaces,
            "dependencies": dependencies,
            "hubs": hubs,
        }


def print_report(report: Dict) -> None:
    ConsoleOutput.section(f"TFC Discovery: {report['organization']}")
    ConsoleOutput.info(f"Total workspaces: {report['total_workspaces']}")

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
        for edge in dependencies:
            label = "remote state" if edge["type"] == "remote_state_consumer" else "run trigger"
            print(f"  {edge['from']} -> {edge['to']}  [{label}]")
    else:
        ConsoleOutput.info("None found.")

    hubs = report["hubs"]
    if hubs:
        ConsoleOutput.section("Hub workspaces (most dependents)")
        for hub in hubs[:10]:
            print(f"  {hub['name']}: {hub['dependent_count']} dependent(s)")


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

        service = DiscoveryService(tfc, args.tfc_organization, project_id)
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


if __name__ == "__main__":
    main()
