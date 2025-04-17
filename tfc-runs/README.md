# TFC Runs
Get the total number of runs ever executed in a Terraform Cloud account. As far as we know, the TFC API does not provider a date filter to break the run count down by month or year, but the goal of this is to give you a starting point to help with Scalr run estimation.

Scalr does not charge for all runs. This script will return some runs for which you will not be charged in Scalr. Use this as a worst-case scenario for estimation purposes. See more on billable runs [here](https://docs.scalr.io/docs/pricing-faq#what-runs-do-not-count-toward-billing).

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
