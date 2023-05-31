# TFC Runs
Get the total number of runs executed in TFC

## Requirements:
* `python3` and `pip3`
* `pip3 install click requests --quiet`
* `terraform login` has been performed from the the device this is being exeuted on (script uses the well known credentials file)

## Usage

`python3 runs.py list-all`

### Expected Result

```
python3 runs.py list-all

Workspace demo1 has had 2 runs.
Workspace VPC has had 5 runs.
Workspaces3 has had 0 runs.
Workspace MigrationDemo has had 2 runs.
Workspace Scalr has had 1 runs.
---------------------------
The total runs count across all organizations: 10
```
