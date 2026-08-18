# TFC Discovery

> **Beta.** This is read-only and doesn't touch Scalr or migrate anything, but it's new and hasn't been run against a wide variety of TFC/E organizations yet. Treat its output (especially the `Recommendation` column) as a starting point for planning, not a final answer - verify before acting on it, and please report anything that looks wrong.

Read-only pre-migration scan of a Terraform Cloud/Enterprise organization. It does not touch Scalr and does not migrate anything - it's meant to be run before `../migrate.sh` to help plan the migration:

- **Workspaces with no state** - no resources under management, likely safe to skip migrating.
- **Cross-workspace dependencies** - edges from remote state consumers and run triggers, plus a ranked list of "hub" workspaces (most dependents) to help decide migration order.
- **Stale workspaces** (opt-in via `--stale-days`) - workspaces that *do* have resources under management but haven't had a run recently. These are still being paid for either way; this surfaces the ones that may not be worth the cost of managing at all, migrated or not.

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
  --csv report.csv \
  --stale-days 180
```

- `--tfc-project`: scope discovery to one project instead of the whole organization.
- `--json report.json`: also write the full report (every dependency edge, not just the top 10 hubs) to a file.
- `--csv report.csv`: write one row per workspace to a CSV, sorted by project then workspace name: `TFC Project`, `Workspace`, `Has State` (`Yes`/`No`), `Resource Count` (number of resources in that workspace's state), `Depends On` (what this workspace depends on), `Dependents` (what depends on this workspace), `Dependent Count` (numeric, so you can sort to find hubs), `Recommendation` - a computed call to make (`Migrate`, `Migrate first - N dependents`, `Skip - no state, not referenced by other workspaces`, or `Review - no state, but N workspace(s) depend(s) on it`) - and `Last Run` / `Days Since Last Run` / `Stale`, populated only when `--stale-days` is used.
- `--stale-days N`: flag workspaces that have state but no run in at least N days (or ever). This costs one extra API call per workspace that has state, so it's opt-in - omit the flag to skip the check entirely. Only applies to workspaces with state; an empty workspace is already covered by `Recommendation`, not this.

`--stale-days` is meant to answer a different question than migration order: not "what should I migrate first" but "is this workspace worth paying for at all." A workspace can be a low-priority migration candidate (no dependents) and still be actively used, or a high-priority hub and also stale - the two are independent signals, so they're reported as separate columns rather than folded into `Recommendation`.

You can also invoke the Python script directly instead of the wrapper: `python3 discover.py ...`.

### Expected Result

```
TFC Discovery: my-org
======================

[INFO] Total workspaces: 6
[INFO] Total resources under management: 143

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

With `--stale-days 180`, an additional section lists workspaces with state that haven't run recently:

```
Stale workspaces with state, no runs in 180+ days (2)
======================================================

  - old-app  (34 resource(s), last run 412 days ago)
  - imported-manually  (6 resource(s), never run)
[INFO] 40 resource(s) across these workspaces haven't been touched recently - worth checking whether
they're still needed, since they're being paid for either way.
```

With `--csv report.csv --stale-days 180`, the file looks like:

```
TFC Project,Workspace,Has State,Resource Count,Depends On,Dependents,Dependent Count,Recommendation,Last Run,Days Since Last Run,Stale
Networking,network-hub,Yes,12,,app-1; app-2,2,Migrate first - 2 dependents,2026-07-10T12:00:00Z,3,No
Applications,app-1,Yes,34,network-hub,app-2,1,Migrate first - 1 dependent,2025-06-01T09:00:00Z,412,Yes
Applications,app-2,No,0,app-1; network-hub,,0,"Skip - no state, not referenced by other workspaces",,,
```

(`app-2` has no state, so it's outside the scope of the staleness check - blank, not "No".)
