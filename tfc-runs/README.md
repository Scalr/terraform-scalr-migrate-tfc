# TFC Runs
Get the total number of runs ever executed in a Terraform Cloud account.

Scalr does not charge for all runs. This script will return some runs for which you will not be charged in Scalr. Use this as a worst-case scenario for estimation purposes. See more on billable runs [here](https://docs.scalr.io/docs/pricing-faq#what-runs-do-not-count-toward-billing).

## Requirements:
* `python3` and `pip3`
* `pip3 install click requests --quiet`
* `terraform login` has been performed from the device this is being executed on (script uses the well known credentials file)

## Usage

`python3 runs.py list-all`

Use the `--period` argument to get the number of runs for a specified period (in days)

### Expected Result

```
python3 runs.py list-all

Workspace athena-us-east has had 22 runs.
Workspace vpc-dev has had 109 runs.
Workspace vpc-prod has had 120 runs.
Workspace taco-us-west has had 523 runs.
Workspace vending-machine has had 78 runs.
---------------------------
The total runs count across all organizations: 852
```
