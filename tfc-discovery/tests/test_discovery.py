"""Tests for DiscoveryService (no-state detection + dependency graph building).

Run from the repo root (scalr_tfc_migrate must be importable):
    pip install pytest
    python3 -m pytest tfc-discovery/tests/test_discovery.py -v

Uses a fake TFC client (duck-typed, no network) so these run without
credentials. Fixture graph:
    A --remote_state_consumer--> C
    B --run_trigger------------> C
A and C have state; B does not.
"""
import csv

from scalr_tfc_migrate.discovery import DiscoveryService, workspace_has_state, workspace_project_id, write_csv


def _workspace(ws_id, name, has_state, project_id=None):
    relationships = {}
    if has_state:
        relationships["current-state-version"] = {"links": {"related": f"/state-versions/{ws_id}"}}
    else:
        relationships["current-state-version"] = {}
    if project_id:
        relationships["project"] = {"data": {"id": project_id, "type": "projects"}}
    return {"id": ws_id, "type": "workspaces", "attributes": {"name": name}, "relationships": relationships}


class FakeTFCClient:
    """Duck-typed stand-in for TFCClient - only implements what DiscoveryService calls."""

    def __init__(self, workspaces, consumers_by_id, inbound_triggers_by_id, projects=None):
        self.workspaces = workspaces
        self.consumers_by_id = consumers_by_id
        self.inbound_triggers_by_id = inbound_triggers_by_id
        self.projects = projects or []

    def get_workspaces(self, org_name, page=1, project_id=None):
        assert page == 1  # single page in these fixtures
        return {"data": self.workspaces, "meta": {"pagination": {"total-pages": 1}}}

    def list_projects(self, org_name, page=1):
        assert page == 1  # single page in these fixtures
        return {"data": self.projects, "meta": {"pagination": {"total-pages": 1}}}

    def get_remote_state_consumers(self, workspace_id, page=1):
        return {"data": self.consumers_by_id.get(workspace_id, []), "meta": {"pagination": {"total-pages": 1}}}

    def get_run_triggers(self, workspace_id, trigger_type="inbound", page=1):
        assert trigger_type == "inbound"
        return {"data": self.inbound_triggers_by_id.get(workspace_id, []), "meta": {"pagination": {"total-pages": 1}}}


def _project(project_id, name):
    return {"id": project_id, "type": "projects", "attributes": {"name": name}}


def _run_trigger(source_id, workspace_id):
    return {
        "id": f"rt-{source_id}-{workspace_id}",
        "type": "run-triggers",
        "relationships": {
            "sourceable": {"data": {"id": source_id, "type": "workspaces"}},
            "workspace": {"data": {"id": workspace_id, "type": "workspaces"}},
        },
    }


def test_workspace_has_state():
    assert workspace_has_state(_workspace("ws-a", "A", has_state=True)) is True
    assert workspace_has_state(_workspace("ws-b", "B", has_state=False)) is False
    assert workspace_has_state({"id": "ws-x", "attributes": {"name": "X"}}) is False  # no relationships key at all


def test_discover_finds_no_state_workspaces_and_dependency_graph():
    workspaces = [
        _workspace("ws-a", "A", has_state=True),
        _workspace("ws-b", "B", has_state=False),
        _workspace("ws-c", "C", has_state=True),
    ]
    consumers_by_id = {
        "ws-a": [{"id": "ws-c", "type": "workspaces"}],  # A's state is consumed by C
    }
    inbound_triggers_by_id = {
        "ws-c": [_run_trigger(source_id="ws-b", workspace_id="ws-c")],  # C is triggered by B
    }

    fake_tfc = FakeTFCClient(workspaces, consumers_by_id, inbound_triggers_by_id)
    report = DiscoveryService(fake_tfc, "my-org").discover()

    assert report["organization"] == "my-org"
    assert report["total_workspaces"] == 3

    assert report["no_state_workspaces"] == [{"id": "ws-b", "name": "B"}]

    dep_pairs = {(d["from"], d["to"], d["type"]) for d in report["dependencies"]}
    assert dep_pairs == {
        ("A", "C", "remote_state_consumer"),
        ("B", "C", "run_trigger"),
    }

    hub_names = {h["name"] for h in report["hubs"]}
    assert hub_names == {"A", "B"}
    assert all(h["dependent_count"] == 1 for h in report["hubs"])


def test_discover_dedupes_duplicate_edges():
    # Same producer/consumer pair showing up on two separate pages (or via both
    # relationship queries) should only produce one edge.
    workspaces = [
        _workspace("ws-a", "A", has_state=True),
        _workspace("ws-c", "C", has_state=True),
    ]
    consumers_by_id = {
        "ws-a": [{"id": "ws-c", "type": "workspaces"}, {"id": "ws-c", "type": "workspaces"}],
    }
    fake_tfc = FakeTFCClient(workspaces, consumers_by_id, {})
    report = DiscoveryService(fake_tfc, "my-org").discover()

    assert len(report["dependencies"]) == 1
    assert report["dependencies"][0]["from"] == "A"
    assert report["dependencies"][0]["to"] == "C"


def test_discover_ignores_self_referencing_edges():
    workspaces = [_workspace("ws-a", "A", has_state=True)]
    consumers_by_id = {"ws-a": [{"id": "ws-a", "type": "workspaces"}]}
    fake_tfc = FakeTFCClient(workspaces, consumers_by_id, {})
    report = DiscoveryService(fake_tfc, "my-org").discover()

    assert report["dependencies"] == []


def test_workspace_project_id():
    assert workspace_project_id(_workspace("ws-a", "A", has_state=True, project_id="prj-1")) == "prj-1"
    assert workspace_project_id(_workspace("ws-a", "A", has_state=True)) is None
    assert workspace_project_id({"id": "ws-x"}) is None  # no relationships key at all


def test_discover_builds_per_workspace_rows_with_project_and_dependencies():
    workspaces = [
        _workspace("ws-a", "A", has_state=True, project_id="prj-1"),
        _workspace("ws-b", "B", has_state=False, project_id="prj-2"),
        _workspace("ws-c", "C", has_state=True, project_id="prj-1"),
    ]
    projects = [_project("prj-1", "Networking"), _project("prj-2", "Apps")]
    consumers_by_id = {"ws-a": [{"id": "ws-c", "type": "workspaces"}]}  # A's state is consumed by C
    inbound_triggers_by_id = {"ws-c": [_run_trigger(source_id="ws-b", workspace_id="ws-c")]}  # C triggered by B

    fake_tfc = FakeTFCClient(workspaces, consumers_by_id, inbound_triggers_by_id, projects=projects)
    report = DiscoveryService(fake_tfc, "my-org").discover()

    rows_by_name = {row["name"]: row for row in report["workspaces"]}
    assert rows_by_name["A"]["project"] == "Networking"
    assert rows_by_name["A"]["has_state"] is True
    assert rows_by_name["A"]["depends_on"] == []

    assert rows_by_name["B"]["project"] == "Apps"
    assert rows_by_name["B"]["has_state"] is False
    assert rows_by_name["B"]["depends_on"] == []

    assert rows_by_name["C"]["project"] == "Networking"
    assert rows_by_name["C"]["has_state"] is True
    # C depends on both A (remote state) and B (run trigger)
    assert sorted(rows_by_name["C"]["depends_on"]) == ["A", "B"]


def test_discover_workspace_row_has_empty_project_when_unknown():
    workspaces = [_workspace("ws-a", "A", has_state=True, project_id="prj-missing")]
    fake_tfc = FakeTFCClient(workspaces, {}, {}, projects=[])  # project id not in the projects list
    report = DiscoveryService(fake_tfc, "my-org").discover()

    assert report["workspaces"][0]["project"] == ""


def test_write_csv(tmp_path):
    report = {
        "workspaces": [
            {"project": "Networking", "name": "hub", "id": "ws-a", "has_state": True, "depends_on": []},
            {"project": "Apps", "name": "app-1", "id": "ws-b", "has_state": False, "depends_on": ["hub", "app-2"]},
        ]
    }
    csv_path = tmp_path / "report.csv"
    write_csv(report, str(csv_path))

    with open(csv_path, newline="") as f:
        rows = list(csv.reader(f))

    assert rows[0] == ["TFC Project", "Workspace", "Has State", "Depends On"]
    assert rows[1] == ["Networking", "hub", "Yes", ""]
    assert rows[2] == ["Apps", "app-1", "No", "hub; app-2"]
