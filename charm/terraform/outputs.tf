# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

output "app_name" {
  description = "Name of the deployed application."
  value       = juju_application.openbao.name
}

output "requires" {
  value = {
    openbao-autounseal-requires = "openbao-autounseal-requires"
    ingress                   = "ingress"
    ingress-per-unit          = "ingress-per-unit"
    tls-certificates-access   = "tls-certificates-access"
    tls-certificates-pki      = "tls-certificates-pki"
    logging                   = "logging"
    s3-parameters             = "s3-parameters"
    tracing                   = "tracing"
  }
}

output "provides" {
  value = {
    openbao-autounseal-provides = "openbao-autounseal-provides"
    openbao-kv                  = "openbao-kv"
    openbao-pki                 = "openbao-pki"
    metrics-endpoint          = "metrics-endpoint"
    send-ca-cert              = "send-ca-cert"
    grafana-dashboard         = "grafana-dashboard"
  }
}
