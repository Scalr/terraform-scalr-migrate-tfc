resource "null_resource" "install-requirements" {
  triggers = {
    time = timestamp()
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/install.sh"
  }
}

locals {
  environment = length(var.scalr_environment) > 0  ? "--scalr-environment \"${var.scalr_environment}\"" : ""
  lock_tfc = var.lock_tf_workspace == true ? "--lock" : ""
  skip_workspace_creation = var.skip_workspace_creation == true ? "--skip-workspace-creation" : ""
  skip_secrets = var.skip_backend_secrets == true ? "--skip-backend-secrets" : ""
  workspaces = join(",", var.workspaces)
}

resource "null_resource" "migrate" {
  triggers = {
    organization = var.tf_organization
    workspaces = local.workspaces
  }

  provisioner "local-exec" {
    command = <<EOF
    python3 ${path.module}/migrator.py migrate --tf-hostname="${var.tf_hostname}" \
        --tf-token "${var.tf_token}" \
        --tf-organization "${var.tf_organization}" \
        --scalr-hostname "${var.scalr_hostname}" \
        --scalr-token "${var.scalr_token}" \
        -a "${var.scalr_account_id}" \
        -v "${var.scalr_vcs_provider_id}" \
        ${local.environment} ${local.lock_tfc} ${local.skip_workspace_creation} ${local.skip_secrets} \
        -w "${local.workspaces}"
    EOF
  }

  depends_on = [null_resource.install-requirements]
}
