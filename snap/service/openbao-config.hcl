ui = true

storage "file" {
  path = "/var/snap/openbao/common/data"
}

# HTTP listener
listener "tcp" {
  address       = "0.0.0.0:8200"
  tls_disable   = 1
}

# To use PKCS#11, you will also need to configure the
# seal: https://openbao.org/docs/configuration/seal/pkcs11/
plugin_directory = "/snap/openbao/current/plugins"
plugin_auto_register = true

plugin "kms" "pkcs11" {
  command   = "openbao-plugin-kms-pkcs11"
  version   = "v0.1.0"
  sha256sum = "__OPENBAO_PKCS11_PLUGIN_SHA256__"
}
