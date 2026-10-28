ui      = true
storage "raft" {
  path= "/openbao/raft"
  node_id = "whatever-openbao-k8s/0"
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
seal "pkcs11" {
  lib = "/var/snap/openbao/common/hsm/pkcs11.so"
  slot = "0"
  token_label = "OpenBao"
  pin = "1234"
  key_label = "bao-root-key"
  key_id = "0x01"
}
