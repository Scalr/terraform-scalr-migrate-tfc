# TFC Discovery

> **Beta.** This is read-only and doesn't touch Scalr or migrate anything, but it's new and hasn't been run against a wide variety of TFC/E organizations yet. Treat its output (especially the `Recommendation` column) as a starting point for planning, not a final answer - verify before acting on it, and please report anything that looks wrong.

Read-only pre-migration scan of a Terraform Cloud/Enterprise organization. It does not touch Scalr and does not migrate anything - it's meant to be run before `../migrate.sh` to help plan the migration:

- **Workspaces with no state** - no resources under management, likely safe to skip migrating.
- **Cross-workspace dependencies** - edges from remote state consumers and run triggers, plus a ranked list of "hub" workspaces (most dependents) to help decide migration order.

## Requirements

- Python 3.12+ (same requirement as the migrator; `discover.sh` auto-detects `python3.12`/`python3`/`python`)
- Terraform Cloud/Enterprise credentials
- No `pip install` needed - this reuses the migrator's shared code (`scalr_tfc_migrate`) one level up, which has no third-party dependencies.

## Usage

```bash
./discover.sh --tfc-token "your-token" --tfc-organization "my-org"
```

`--tfc-hostname` defaults to `app.terraform.io`. Authentication follows the same precedence as `migrate.sh`: command-line flags, then environment variables (`TFC_HOSTNAME`, `TFC_TOKEN`, `TFC_ORGANIZATION`, `TFC_PROJECT`), then `~/.terraform.d/credentials.tfrc.json` (populated by `terraform login`). Both `--flag value` and `--flag=value` forms work.

Optional flags:

```bash
./discover.sh --tfc-token "your-token" --tfc-organization "my-org" \
  --tfc-project "my-project" \
  --json report.json \
  --csv report.csv
```

- `--tfc-project`: scope discovery to one project instead of the whole organization.
- `--json report.json`: also write the full report (every dependency edge, not just the top 10 hubs) to a file.
- `--csv report.csv`: write one row per workspace to a CSV, sorted by project then workspace name: `TFC Project`, `Workspace`, `Has State` (`Yes`/`No`), `Depends On` (what this workspace depends on), `Dependents` (what depends on this workspace), `Dependent Count` (numeric, so you can sort to find hubs), and `Recommendation` - a computed call to make (`Migrate`, `Migrate first - N dependents`, `Skip - no state, not referenced by other workspaces`, or `Review - no state, but N workspace(s) depend(s) on it`) rather than raw data you'd otherwise have to combine yourself.

You can also invoke the Python script directly instead of the wrapper: `python3 discover.py ...`.

### Expected Result

```
TFC Discovery: my-org
======================

[INFO] Total workspaces: 6

Workspaces with no state (1)
=============================

  - placeholder-ws (ws-abc123)
[INFO] These have no resources under management and likely don't need to be migrated.

Cross-workspace dependencies (3)
=================================

  network-hub  (2 dependents)
    -> app-1  [remote state]
    -> app-2  [remote state]
  app-1  (1 dependent)
    -> app-2  [run trigger]
```

Dependencies are grouped by source workspace and sorted by dependent count (most first), so hubs and everything depending on them read as one block instead of a flat edge list next to a separately-ranked hub list.

With `--csv report.csv`, the file looks like:

```
TFC Project,Workspace,Has State,Depends On,Dependents,Dependent Count,Recommendation
Networking,network-hub,Yes,,app-1; app-2,2,Migrate first - 2 dependents
Applications,app-1,Yes,network-hub,app-2,1,Migrate first - 1 dependent
Applications,app-2,No,app-1; network-hub,,0,"Skip - no state, not referenced by other workspaces"
```
