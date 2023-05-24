resource "null_resource" "install-requirements" {
  triggers = {
    time = timestamp()
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/install.sh"
  }
}

locals {
  lock_tfc_workspace = var.lock_tf_workspace == true ? "--lock" : ""
  workspaces = join(",", var.workspaces)
}

resource "null_resource" "migrate" {
  triggers = {
    organization = var.tf_organization
    workspaces = local.workspaces
  }

  provisioner "local-exec" {
    command = <<EOF
    python3 ${path.module}/migrator.py migrate --tf-hostname=${var.tf_hostname} \
    --tf-token=${var.tf_token} \
    --tf-organization=${var.tf_organization} \
    --scalr-hostname=${var.scalr_hostname} \
    --scalr-token=${var.scalr_token} \
    --scalr-environment=${var.scalr_environment} \
    --skip-workspace-creation=${var.skip_workspace_creation} \
    --skip-backend-secrets=${var.skip_backend_secrets} \
    -a ${var.scalr_account_id} \
    -v ${var.scalr_vcs_provider_id} \
    -w "${local.workspaces}" \
    ${local.lock_tfc_workspace}
    EOF
  }

  depends_on = [null_resource.install-requirements]
}
