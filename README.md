Migration TFC/E to Scalr
========================

This module helps Terraform Cloud/Enterprise users migrate their workspaces to [Scalr](https://scalr.com) remote backend.

Before the migration, please do the following:

* Obtain a TFC/E personal or team access token. This can be done in two ways: [manually](https://app.terraform.io/app/settings/tokens) or via [terraform login](https://www.terraform.io/cli/commands/login).
* Obtain a Scalr access token. The easiest way is to use the `terraform login account-name.scalr.io` command.
* Register a [VCS provider] (https://docs.scalr.io/docs/integrations) in Scalr. The registered provider must have access to all repositories connected to the TFC/E workspaces. After the provider is created, copy the VCS provider ID.
* Obtain the Scalr account identifier. It can be found on the account dashboard.

What Terraform Cloud/Enterprise objects will be migrated:

* Organizations - Will be migrated into [Scalr environments](https://docs.scalr.com/en/latest/hierarchy.html#environments)
* Workspaces - Will be migrated into [Scalr workspaces](https://docs.scalr.com/en/latest/workspaces.html). Only VCS-based workspaces will be migrated. CLI-driven workspaces have to be [migrated manually](https://docs.scalr.com/en/latest/migration.html).  
* Workspace variables - Terraform and non-sensitive environment variables will be created as Terraform and shell variables at the workspace level.
* State files - The current state file of a workspace will be migrated to Scalr state storage.

Usage
-----

* Assuming you will use the Terraform CLI, create a main.tf locally.
* Then copy and paste the following source code and fill in the required inputs: 

```hcl
module "migrator" {
  source = "github.com/Scalr/terraform-scalr-migrate-tfc"
  
  # required inputs
  tf_token = "<tfc-token>"
  tf_organization = "<tf-organization-name>"

  scalr_account_id = "<scalr-account-id>"
  scalr_hostname = "<scalr-hostname>"
  scalr_token = "<scalr-token>"
  scalr_vcs_provider_id = "<scalr-vcs-id>"
  
  # optional inputs
  # By default, it takes the TFC/E organization name to name a Scalr environment after. 
  # But users could set a custom environment name
  scalr_environment = "<scalr-environment-ID>" 
  # By default, the tool migrates all Terraform Cloud/Enterprise workspaces, but the user can control which workspaces should be migrated into Scalr.
  workspaces = ["*"]
  # By default, the tool locks Terraform Cloud/Enterprise workspaces to keep a single source of state
  lock_tf_workspace = true
}
```

* Run `terraform init` and then `terraform apply`
* After the migration is done you still need to configure the [provider configurations](https://docs.scalr.io/docs/provider-configurations) or sensitive shell variables to authorize your pipelines.
* After the secrets configuration is done, trigger the run to double-check workspaces work as expected and generate no changes.
