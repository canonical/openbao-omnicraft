ui      = true
audit "file" "charm-stdout" {
  description = "Audit device managed by the charm"
  options {
    file_path = "stdout"
  }
}
storage "raft" {
  path= "/openbao/raft"
  node_id = "whatever-openbao-k8s/0"
}
log_level = info
listener "tcp" {
  telemetry {
    unauthenticated_metrics_access = true
  }
  address       = "[::]:8200"
  tls_cert_file = "/openbao/certs/cert.pem"
  tls_key_file  = "/openbao/certs/key.pem"
}
default_lease_ttl = "168h"
max_lease_ttl     = "720h"
disable_mlock     = true
cluster_addr      = "https://myhostname:8201"
api_addr          = "https://myhostname:8200"
telemetry {
  disable_hostname = true
  prometheus_retention_time = "12h"
}
