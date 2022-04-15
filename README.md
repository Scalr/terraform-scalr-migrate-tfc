Migration TFC/E to Scalr
========================

This module helps Terraform Cloud/Enterprise users to migrate their workspaces to [Scalr](https://scalr.com) remote backend.

Prior to the migration user has to:

* obtain a TFC/E access token. It can be done in two ways: [manually](https://app.terraform.io/app/settings/tokens) or via [terraform login](https://www.terraform.io/cli/commands/login).
* obtain a Scalr access token. It can be done in two ways: [manually](https://scalr.io/app/settings/tokens) or via [terraform login account-name.scalr.io](https://www.terraform.io/cli/commands/login).
* Register a [VCS provider](https://docs.scalr.com/en/latest/vcs_providers.html) in Scalr. Note that the registered provider must have the access to all repositories connected to the TFC/E workspaces. After the provider is created, the user has to copy a VCS provider id.
* Obtain the Scalr account identifier. It could be taken from the account dashboard.

What Terraform Cloud/Enterprise entities will be migrated:

* organizations - will be migrated into the [Scalr environments](https://docs.scalr.com/en/latest/hierarchy.html#environments)
* workspaces - will be migrated into the [Scalr workspaces](https://docs.scalr.com/en/latest/workspaces.html). VCS workspaces are migrated only. CLI-driven workspaces have to be [migrated manually](https://docs.scalr.com/en/latest/migration.html).  
* workspace variables - all Terraform and non-sensitive Environment variables will be created as Terraform and Shell variables in Scalr.
* State files - The current state file of a workspace will be taken and pushed into the Scalr state storage.

Usage
-----

```hcl
module "migrator" {
  source = "github.com/emocharnik/terraform-migrate-tfc-scalr"
  
  # required inputs
  tf_token = "<tfc-token>"
  scalr_account_id = "<scalr-account-id>"
  scalr_hostname = "<scalr-hostname>"
  scalr_token = "<scalr-token>"
  scalr_vcs_provider_id = "<scalr-vcs-id>"
  
  # optional inputs
  # by default, the tool locks Terraform Cloud/Enterprise workspaces in order to keep a single source of state
  lock_tf_workspace = true
  # by default, the tool migrates all Terraform Cloud/Enterprise organizations, but the user can control 
  # which organizations do not migrate into Scalr, e.g. organization that manages other Terraform Cloud organizations
  ignore_organizations = []
}
```
