# Configure PKCS#11 HSM auto-unseal (Machine)

Use a PKCS#11 hardware security module (HSM), such as SoftHSM or a YubiKey, to auto-unseal the OpenBao machine charm.

**This change is one-way.** After OpenBao is initialized with the PKCS#11 seal, you cannot return to Shamir unseal keys by unsetting the configuration or removing the library. Doing so can leave the cluster unable to unseal. There is currently no charm-supported migration back to Shamir.

Do not combine PKCS#11 auto-unseal with [transit auto-unseal](configure_for_autounseal.md). The charm will go to `blocked` if both are configured.

## Warnings

- Configure PKCS#11 **before** initializing OpenBao. If the cluster is already initialized with Shamir, switching to PKCS#11 requires a seal migration (`bao operator unseal -migrate`) and is easy to get wrong.
- Initialization with PKCS#11 returns **recovery keys**, not Shamir unseal keys. Store the recovery keys. They are not used for day-to-day unseal.
- The HSM PIN is written to the OpenBao configuration file on each unit (`/var/snap/openbao/common/openbao-config.hcl`). Protect the machine accordingly.
- OpenBao does not create HSM keys. You must create the AES or RSA key on the token before enabling this feature. See the [OpenBao PKCS#11 seal documentation](https://openbao.org/docs/configuration/seal/pkcs11/).
- Every unit must be able to reach the same HSM (or an equivalent network HSM) and load the same PKCS#11 library. A USB token attached to a single machine will not unseal other units.
- The charm installs the attached `hsm-lib` directory into snap-common so the strictly confined OpenBao snap can load the module and its dependencies. Host paths such as `/usr/lib/...` are not visible inside the snap. Device access (for example `pcscd` and USB) may still need extra snap connections.
- The OpenBao snap keeps a **static** `bao` binary and ships the external **PKCS#11 KMS plugin** (`openbao-plugin-kms-pkcs11`) under `/snap/openbao/current/plugins/`. PKCS#11 is available on **amd64** and **arm64** snap builds only. The charm blocks if the installed snap revision does not include the plugin.

## Prerequisites

1. An OpenBao machine charm deployment that is **not** yet initialized, or a cluster you intend to migrate.
2. A directory containing the PKCS#11 module and any shared libraries it needs (for example SoftHSM `libsofthsm2.so`, or YubiHSM `yubihsm_pkcs11.so` plus `libyubihsm*.so*`).
3. A token PIN, and either a slot or a token label, and either a key label or a key ID.
4. An amd64 or arm64 OpenBao snap revision that ships `plugins/openbao-plugin-kms-pkcs11`.

## SoftHSM smoke path

Prefer SoftHSM (or another PKCS#11 provider with few dependencies) for first validation.

1. Install SoftHSM on the host and create a token plus AES/RSA key.
2. Put `libsofthsm2.so` (and any required deps) in a directory, pack it, attach it as `hsm-lib`, create the HSM secret, and set `hsm-config-secret-id` as below.
3. Confirm the unit leaves `blocked`, OpenBao initializes with seal type `pkcs11`, and a restart auto-unseals.

For snap-only (non-charm) PKCS#11 setup and common failure modes, see
[snap/TROUBLESHOOTING.md](https://github.com/canonical/openbao-omnicraft/blob/main/snap/TROUBLESHOOTING.md).

## 1. Pack and attach the PKCS#11 library directory

Juju file resources are a single file, so pack the library directory as a tarball:

```bash
mkdir -p ./hsm-libs
# Copy the PKCS#11 module and its shared-library dependencies into ./hsm-libs
# Prefer naming the module pkcs11.so, or set secret key `lib` to the real filename.
tar czf hsm-lib.tar.gz -C ./hsm-libs .
juju attach-resource openbao hsm-lib=./hsm-lib.tar.gz
```

The charm extracts the archive to `/var/snap/openbao/common/hsm/` and points the seal `lib` at the PKCS#11 module. A single ELF `.so` is still accepted for simple providers.

The charm ignores a deploy-time placeholder (non-ELF text). Attach the real tarball before setting the secret.

When deploying a local `.charm` file, Juju requires the resource at deploy time. Use the placeholder shipped in the repository until you attach a real library:

```bash
juju deploy ./openbao.charm --resource hsm-lib=./hsm-lib-placeholder.tar.gz
juju attach-resource openbao hsm-lib=./hsm-lib.tar.gz
```

## 2. Create and grant the HSM secret

```bash
juju add-secret hsm-config \
  slot=<string> \
  token-label=<string> \
  pin=<string> \
  key-label=<string> \
  key-id=<string> \
  lib=<module-filename>
juju grant-secret hsm-config openbao
```

`pin` is required. Provide `slot` and/or `token-label`, and `key-label` and/or `key-id`. Omit unused keys.

`lib` is optional when the archive contains a top-level `pkcs11.so`, or exactly one top-level ELF `.so`. Set it when there are multiple shared objects (for example `lib=yubihsm_pkcs11.so`).

## 3. Point the charm at the secret

```bash
juju config openbao hsm-config-secret-id=<secret-id>
```

Use the secret ID printed by `juju add-secret` (for example `secret:cqgj49fmp25c7796r0pg`).

The charm registers the snap's PKCS#11 KMS plugin and adds a `seal "pkcs11"` stanza to the OpenBao configuration.

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
