# TFC Discovery

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
- `--csv report.csv`: write one row per workspace to a CSV with columns `TFC Project`, `Workspace`, `Has State` (`Yes`/`No`), and `Depends On` (names of any workspace(s) it depends on via remote state consumption or a run trigger, semicolon-separated if more than one, empty if none).

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
TFC Project,Workspace,Has State,Depends On
Networking,network-hub,Yes,
Applications,app-1,Yes,network-hub
Applications,app-2,No,app-1; network-hub
```

## Tests

```bash
cd ..  # repo root - scalr_tfc_migrate must be importable
pip install pytest
python3 -m pytest tfc-discovery/tests/ -v
```

Runs against a duck-typed fake TFC client (no network or credentials needed) covering no-state detection, edge building from both dependency sources, de-duplication, self-reference filtering, project-name resolution, per-workspace dependency rows, CSV output, and request-timeout/network-error handling.
