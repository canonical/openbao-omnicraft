# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

resource "juju_application" "openbao-k8s" {
  name  = var.app_name
  model_uuid = var.model

  charm {
    name     = "openbao-k8s"
    channel  = var.channel
    revision = var.revision
    base     = var.base
  }

  config      = var.config
  constraints = var.constraints
  units       = var.units
  trust       = var.trust

  storage_directives = var.storage_directives
}
