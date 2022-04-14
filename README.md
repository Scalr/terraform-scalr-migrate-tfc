Migration TFC/E to Scalr
========================

This module helps Terraform Cloud/Enterprise users to migrate their workspaces to [Scalr](https://scalr.com) remote backend.

Prior to the migration user has to:

* obtain a TFC/E access token. It can be done in two ways: [manually](https://app.terraform.io/app/settings/tokens) or via [terraform login](https://www.terraform.io/cli/commands/login).
* obtain a Scalr access token. It can be done in two ways: [manually](https://scalr.io/app/settings/tokens) or via [terraform login <account-name>.scalr.io](https://www.terraform.io/cli/commands/login).
* Register a [VCS provider](https://docs.scalr.com/en/latest/vcs_providers.html) in Scalr. Note that the registered provider must have the access to all repositories connected to the TFC/E workspaces. After the provider is created, user has to copy a VCS provider id.
* Obtain the Scalr account identifier. It could be taken from the account dashboard.

What Terraform Cloud/Enterprise entities will be migrated:

* organizations - will be migrated into the [Scalr environments](https://docs.scalr.com/en/latest/hierarchy.html#environments)
* workspaces - will be migrated into the [Scalr workspaces](https://docs.scalr.com/en/latest/workspaces.html). Both VCS and CLI workspaces will be migrated. 
* workspace variables - all Terraform and non-sensitive Environment variables will be created as Terraform and Shell variables in Scalr.
* State files - The current state file of a workspace will be taken and pushed into the Scalr state storage.
