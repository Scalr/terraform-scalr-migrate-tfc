variable "export_vars_to_scalr" {
  type    = list(string)
  default = []
}

variable "scalr_workspace_id" {
  type    = string
}

data "external" "example" {
  program = ["python3", "${path.module}/migrate-environment.py"]

  query = {
    variables = join(",", var.export_vars_to_scalr)
    workspace_id = var.scalr_workspace_id
  }
}

output "external_result" {
  value = data.external.example.result
}