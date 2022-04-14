resource "null_resource" "install-requirements" {
  triggers = {
    time = "${timestamp()}"
  }

  provisioner "local-exec" {
    command = "bash ${path.root}/install.sh"
  }
}

locals {
  lock_tfc_workspace = var.lock_tfc_workspace == true ? "--lock" : ""
}

resource "null_resource" "migrate" {
  provisioner "local-exec" {
    command = <<EOF
    python3 ${path.root}/migrator.py migrate --tf-hostname=${var.tf_hostname} \
    --tf-token=${var.tf_token} \
    --scalr-hostname=${var.scalr_hostname} \
    --scalr-token=${var.scalr_token} \
    -a ${var.scalr_account_id} \
    -v ${var.scalr_vcs_provider_id} \
    ${local.lock_tfc_workspace}
    EOF
  }

  depends_on = [null_resource.install-requirements]
}
