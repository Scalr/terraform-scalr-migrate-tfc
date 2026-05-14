variable "export_vars_to_scalr" {
  type    = list(string)
  default = []
}

variable "scalr_workspace_id" {
  type    = string
  default = null
}

variable "scalr_var_set_id" {
  type    = string
  default = null
}

data "external" "example" {
  program = ["python3", "${path.module}/migrate-environment.py"]

  query = {
    variables    = join(",", var.export_vars_to_scalr)
    workspace_id = var.scalr_workspace_id != null ? var.scalr_workspace_id : ""
    var_set_id   = var.scalr_var_set_id != null ? var.scalr_var_set_id : ""
  }
}

output "external_result" {
  value = data.external.example.result
}