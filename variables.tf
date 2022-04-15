variable "tf_hostname" {
  type = string
  default = "app.terraform.io"
}

variable "tf_token" {
  type = string
  sensitive = true
}

variable "scalr_hostname" {
  type = string
}

variable "scalr_token" {
  type = string
  sensitive = true
}

variable "scalr_account_id" {
  type = string
}

variable "scalr_vcs_provider_id" {
  type = string
}

variable "lock_tf_workspace" {
  type = bool
  default = true
}

variable "ignore_organizations" {
  type = list(string)
  default = []
}