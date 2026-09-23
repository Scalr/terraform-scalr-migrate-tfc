variable "scalr_provider_configuration_payload" {
  type    = string
  default = ""
}

data "external" "scalr_provider_configuration" {
  program = ["python3", "${path.module}/migrate-provider-configuration.py"]

  query = {
    payload = var.scalr_provider_configuration_payload
  }
}

output "scalr_provider_configuration_result" {
  value = data.external.scalr_provider_configuration.result
}
