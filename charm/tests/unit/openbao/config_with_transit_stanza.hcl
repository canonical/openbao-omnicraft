ui      = true
storage "raft" {
  path= "/openbao/raft"
  node_id = "whatever-openbao-k8s/0"
   retry_join {
    leader_api_addr = "http://127.0.0.1:8200"
    leader_ca_cert_file = "/path/to/ca1"
  }
  retry_join {
    leader_api_addr = "http://127.0.0.2:8200"
    leader_ca_cert_file = "/path/to/ca1"
  }

  }
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
cluster_addr      = "https://1.2.3.4:8201"
api_addr          = "https://1.2.3.4:8200"
telemetry {
  disable_hostname = true
  prometheus_retention_time = "12h"
}
seal "transit" {
  address         = "https://192.168.1.100:8200"
  disable_renewal = "false"
  key_name        = "openbao-unseal-key"
  mount_path      = "openbao-unseal"
  token           = "openbao-unseal-token"
  tls_ca_cert     = "/path/to/ca1"
}