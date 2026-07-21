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

from scalr_tfc_migrate.discovery import (
    DiscoveryService,
    print_report,
    recommend,
    workspace_has_state,
    workspace_project_id,
    write_csv,
)


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
    assert rows_by_name["A"]["dependents"] == ["C"]  # C consumes A's state
    assert rows_by_name["A"]["recommendation"] == "Migrate first - 1 dependent"

    assert rows_by_name["B"]["project"] == "Apps"
    assert rows_by_name["B"]["has_state"] is False
    assert rows_by_name["B"]["depends_on"] == []
    assert rows_by_name["B"]["dependents"] == ["C"]  # C is triggered by B
    assert rows_by_name["B"]["recommendation"] == "Review - no state, but 1 workspace depend(s) on it"

    assert rows_by_name["C"]["project"] == "Networking"
    assert rows_by_name["C"]["has_state"] is True
    # C depends on both A (remote state) and B (run trigger)
    assert sorted(rows_by_name["C"]["depends_on"]) == ["A", "B"]
    assert rows_by_name["C"]["dependents"] == []
    assert rows_by_name["C"]["recommendation"] == "Migrate"

    # Rows are sorted by (project, name) for spreadsheet-friendly default ordering:
    # "Apps" < "Networking" alphabetically, so B comes first, then A/C within Networking.
    assert [row["name"] for row in report["workspaces"]] == ["B", "A", "C"]


def test_recommend():
    assert recommend(has_state=False, dependent_names=[]) == "Skip - no state, not referenced by other workspaces"
    assert recommend(has_state=False, dependent_names=["X"]) == "Review - no state, but 1 workspace depend(s) on it"
    assert recommend(has_state=False, dependent_names=["X", "Y"]) == \
        "Review - no state, but 2 workspaces depend(s) on it"
    assert recommend(has_state=True, dependent_names=[]) == "Migrate"
    assert recommend(has_state=True, dependent_names=["X"]) == "Migrate first - 1 dependent"
    assert recommend(has_state=True, dependent_names=["X", "Y"]) == "Migrate first - 2 dependents"


def test_discover_workspace_row_has_empty_project_when_unknown():
    workspaces = [_workspace("ws-a", "A", has_state=True, project_id="prj-missing")]
    fake_tfc = FakeTFCClient(workspaces, {}, {}, projects=[])  # project id not in the projects list
    report = DiscoveryService(fake_tfc, "my-org").discover()

    assert report["workspaces"][0]["project"] == ""


def test_print_report_groups_dependencies_by_source_hub_first(capsys):
    # Same shape as the real-world case that prompted this: a flat edge list
    # was hard to read against a separately-printed hub ranking. Dependencies
    # should now render grouped under their source, sorted by dependent count
    # (most first), with dependents indented beneath - no separate section.
    report = {
        "organization": "tfc-migration-demo",
        "total_workspaces": 8,
        "no_state_workspaces": [],
        "dependencies": [
            {"from": "ec2_instance", "to": "null_resource_module", "type": "run_trigger"},
            {"from": "testworkspace-1", "to": "null_resource_module", "type": "run_trigger"},
            {"from": "test2", "to": "test1", "type": "run_trigger"},
            {"from": "ec2_instance", "to": "workspace2", "type": "run_trigger"},
        ],
        "hubs": [],
        "workspaces": [],
    }
    print_report(report)
    out = capsys.readouterr().out

    lines = [line for line in out.splitlines() if line.strip()]
    ec2_index = next(i for i, line in enumerate(lines) if "ec2_instance" in line)
    # The 2-dependent hub is grouped first, with both its dependents indented
    # directly beneath it (not interleaved with other sources' edges).
    assert "(2 dependents)" in lines[ec2_index]
    assert "-> null_resource_module" in lines[ec2_index + 1]
    assert "-> workspace2" in lines[ec2_index + 2]
    # No separate "Hub workspaces" section anymore.
    assert "Hub workspaces" not in out


def test_write_csv(tmp_path):
    report = {
        "workspaces": [
            {
                "project": "Networking", "name": "hub", "id": "ws-a", "has_state": True,
                "depends_on": [], "dependents": ["app-1", "app-2"], "recommendation": "Migrate first - 2 dependents",
            },
            {
                "project": "Apps", "name": "app-1", "id": "ws-b", "has_state": False,
                "depends_on": ["hub", "app-2"], "dependents": [], "recommendation": "Skip - no state, not referenced by other workspaces",
            },
        ]
    }
    csv_path = tmp_path / "report.csv"
    write_csv(report, str(csv_path))

    with open(csv_path, newline="") as f:
        rows = list(csv.reader(f))

    assert rows[0] == ["TFC Project", "Workspace", "Has State", "Depends On", "Dependents", "Dependent Count",
                        "Recommendation"]
    assert rows[1] == ["Networking", "hub", "Yes", "", "app-1; app-2", "2", "Migrate first - 2 dependents"]
    assert rows[2] == ["Apps", "app-1", "No", "hub; app-2", "", "0",
                        "Skip - no state, not referenced by other workspaces"]
