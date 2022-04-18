variable "tf_hostname" {
  type = string
  default = "app.terraform.io"
  description = "The hostname of a Terraform Cloud/Enterprise installation. Defaults to Terraform Cloud."
}

variable "tf_token" {
  type = string
  sensitive = true
  description = "The token to authorise requests to a Terraform Cloud/Enterprise installation."
}

variable "scalr_hostname" {
  type = string
  description = "The hostname of a Scalr installation."
}

variable "scalr_token" {
  type = string
  sensitive = true
  description = "The token to authorise requests to a Scalr installation."
}

variable "scalr_account_id" {
  type = string
  description = "The Scalr account identifier to migrate TFC/E data into"
}

variable "scalr_vcs_provider_id" {
  type = string
  description = "The Scalr VCS provider identifier to associate workspaces with."
}

variable "lock_tf_workspace" {
  type = bool
  default = true
  description = "Whether to lock TFC/E workspaces from the runs execution in order to avoid the state conflicts."
}

variable "ignore_organizations" {
  type = list(string)
  default = []
  description = "List of organizations that should not be migrated into Scalr. By default all ones are migrated."
}