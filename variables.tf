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

variable "tf_organization" {
  type = string
  description = "The Terraform Cloud/Enterprise to migrate into Scalr."
}

variable "workspaces" {
  type = list(string)
  default = ["*"]
  description = "List of organizations that should not be migrated into Scalr. By default all ones are migrated."
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

variable "scalr_environment" {
  type = string
  default = ""
  description = <<EOF
    The name of a Scalr environment. By default, it takes the TFC/E organization to name a Scalr environment after.
    But users could set a custom environment name, e.g. if they manage everything in a single organization,
    but want to re-structure their workspaces.
  EOF
}

variable "lock_tf_workspace" {
  type = bool
  default = true
  description = "Whether to lock TFC/E workspaces from the runs execution in order to avoid the state conflicts."
}