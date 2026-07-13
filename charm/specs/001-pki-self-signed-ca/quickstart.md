# Quickstart: PKI Self-Signed CA

**Feature**: PKI Self-Signed CA  
**Date**: 2026-05-25

## Prerequisites

- A Juju model with OpenBao deployed and initialized
- The OpenBao charm has `openbao-pki` relation endpoint available
- No external CA charm (e.g., `self-signed-certificates`) is related to OpenBao's `tls-certificates-pki` endpoint

## Enable Self-Signed PKI

### 1. Configure the CA common name

```bash
juju config openbao pki_ca_common_name="openbao-ca.example.com"
```

### 2. (Optional) Configure additional CA attributes

```bash
juju config openbao pki_ca_sans_dns="openbao-ca.example.com,ca.example.com"
juju config openbao pki_ca_country_name="US"
juju config openbao pki_ca_state_or_province_name="California"
juju config openbao pki_ca_locality_name="San Francisco"
juju config openbao pki_ca_organization="Example Corp"
juju config openbao pki_ca_organizational_unit="Security"
```

### 3. Relate a certificate requirer

```bash
juju deploy tls-certificates-requirer --config common_name="app.example.com"
juju integrate openbao:openbao-pki tls-certificates-requirer:certificates
```

### 4. Verify certificates are issued

```bash
juju status
# The requirer should show "Unit certificate is available"
```

## Switch from External CA to Self-Signed CA

If you previously used an external CA charm:

```bash
# Remove the external CA relation
juju remove-relation openbao:self-signed-certificates

# The charm will automatically transition to self-signed CA mode
# and generate a new CA certificate
```

## Switch from Self-Signed CA to External CA

```bash
# Deploy and relate an external CA charm
juju deploy self-signed-certificates
juju integrate openbao:tls-certificates-pki self-signed-certificates:certificates

# The charm will automatically switch to external CA mode
# and request an intermediate CA from the external provider
```

## Rotate the Self-Signed CA

Change the common name (or any CA attribute) to trigger rotation:

```bash
juju config openbao pki_ca_common_name="openbao-ca-v2.example.com"
```

The charm will:
1. Generate a new self-signed CA
2. Import it as a new issuer in the PKI mount
3. Set it as the default issuer
4. New certificates will be signed by the new CA

## Troubleshooting

### Charm shows "pki_ca_common_name is not set"

```bash
juju config openbao pki_ca_common_name="your-domain.com"
```

### Charm shows blocked status with "tls-certificates-pki relation is missing"

This should NOT happen in self-signed mode. If it does:
- Check that `pki_ca_common_name` is valid
- Check charm logs: `juju debug-log --include openbao`

### Certificates not being issued

- Verify OpenBao is initialized and unsealed
- Check that the `openbao-pki` relation is established
- Check charm logs for PKI role or signing errors
