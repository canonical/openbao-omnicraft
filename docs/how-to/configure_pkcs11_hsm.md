# Configure PKCS#11 HSM auto-unseal (Machine)

Use a PKCS#11 hardware security module (HSM), such as a YubiKey, to auto-unseal the OpenBao machine charm.

**This change is one-way.** After OpenBao is initialized with the PKCS#11 seal, you cannot return to Shamir unseal keys by unsetting the configuration or removing the library. Doing so can leave the cluster unable to unseal. There is currently no charm-supported migration back to Shamir.

Do not combine PKCS#11 auto-unseal with [transit auto-unseal](configure_for_autounseal.md). The charm will go to `blocked` if both are configured.

## Warnings

- Configure PKCS#11 **before** initializing OpenBao. If the cluster is already initialized with Shamir, switching to PKCS#11 requires a seal migration (`bao operator unseal -migrate`) and is easy to get wrong.
- Initialization with PKCS#11 returns **recovery keys**, not Shamir unseal keys. Store the recovery keys. They are not used for day-to-day unseal.
- The HSM PIN is written to the OpenBao configuration file on each unit (`/var/snap/openbao/common/openbao-config.hcl`). Protect the machine accordingly.
- OpenBao does not create HSM keys. You must create the AES or RSA key on the token before enabling this feature. See the [OpenBao PKCS#11 seal documentation](https://openbao.org/docs/configuration/seal/pkcs11/).
- Every unit must be able to reach the same HSM (or an equivalent network HSM) and load the same PKCS#11 library. A USB token attached to a single machine will not unseal other units.
- The charm copies the attached library into snap-common so the strictly confined OpenBao snap can read it. Any further libraries the `.so` needs, plus device access (for example `pcscd` and USB for a YubiKey), must be available on the host. Connect extra snap interfaces if the workload cannot talk to the HSM.
- The OpenBao snap must be built with PKCS#11 support (an HSM/cgo build). A static OpenBao binary will not load this seal.

## Prerequisites

1. An OpenBao machine charm deployment that is **not** yet initialized, or a cluster you intend to migrate.
2. A PKCS#11 library file (for example `libykcs11.so`).
3. A token PIN, and either a slot or a token label, and either a key label or a key ID.

## 1. Attach the PKCS#11 library

```bash
juju attach-resource openbao hsm-lib=./some-lib.so
```

The charm ignores a placeholder (non-ELF) file. Attach the real shared library before setting the secret.

When deploying a local `.charm` file, Juju requires the resource at deploy time. Use the placeholder shipped in the repository until you attach a real library:

```bash
juju deploy ./openbao.charm --resource hsm-lib=./hsm-lib-placeholder
juju attach-resource openbao hsm-lib=./some-lib.so
```

## 2. Create and grant the HSM secret

```bash
juju add-secret hsm-config \
  slot=<string> \
  token-label=<string> \
  pin=<string> \
  key-label=<string> \
  key-id=<string>
juju grant-secret hsm-config openbao
```

`pin` is required. Provide `slot` and/or `token-label`, and `key-label` and/or `key-id`. Omit unused keys.

## 3. Point the charm at the secret

```bash
juju config openbao hsm-config-secret-id=<secret-id>
```

Use the secret ID printed by `juju add-secret` (for example `secret:cqgj49fmp25c7796r0pg`).

The charm copies the library to `/var/snap/openbao/common/hsm/pkcs11.so` and adds a `seal "pkcs11"` stanza to the OpenBao configuration.

## 4. Initialize OpenBao

Wait until units report `Please initialize OpenBao`, then initialize as in [Getting started (Machine)](../tutorial/getting_started_machine.md). Use recovery keys from this initialization, not Shamir unseal keys.

Authorize the charm with a short-lived root token as usual.

After a restart, units should unseal automatically using the HSM. You should not need `bao operator unseal` for routine restarts.

## Migrating an existing Shamir cluster

If OpenBao is already initialized with Shamir:

1. Attach the library, create the secret, and set `hsm-config-secret-id` as above.
2. When units report that a migration is required, unseal with the **existing Shamir keys** and `-migrate`:

```bash
bao operator unseal -migrate ${unseal_key}
```

After a successful migration, Shamir unseal keys no longer unseal the cluster. Keep the recovery keys from the new seal.

## Key rotation

Generate a new key on the token with a new label, update the Juju secret, and wait for the charm to rewrite the configuration. Do not delete old keys; OpenBao still needs them to decrypt older data. See the upstream [key rotation notes](https://openbao.org/docs/configuration/seal/pkcs11/).
