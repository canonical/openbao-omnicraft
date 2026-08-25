# Configure a OpenBao for auto-unseal 

**WARNING: There is currently no way to remove the auto-unseal configuration once it has been set on OpenBao Charms. Removing the integration may put OpenBao Charms in a bad state which requires manual intervention.**

Transit auto-unseal cannot be combined with [PKCS#11 HSM auto-unseal](configure_pkcs11_hsm.md) on the machine charm.


## Prerequisites

1. A OpenBao Charm instance you wish to use as the *unsealer*. Deployed, initialized, unsealed, and authorized. See [Tutorial: Getting started with OpenBao-K8s](../tutorial/getting_started_k8s.md) or [Getting Started: OpenBao (Machine)](../tutorial/getting_started_machine.md) if you're not there yet.
2. A second OpenBao Charm instance you wish to use as the *autounsealed* OpenBao. This instance may already be initialized, unsealed, and authorized, or you may initialize it as part of this process.

## 1. Integrate the OpenBao instances

Integrate the *autounsealed* OpenBao instance with the *unsealer* OpenBao instance.

```bash
juju integrate openbao-unsealer:openbao-autounseal-provides openbao-autounsealed:openbao-autounseal-requires
```

## 2. Configure the OpenBao CLI to interact with the *autounsealed* OpenBao.

```bash
export OPENBAO_ADDR="..."
export BAO_TOKEN="..."
```

Now, either follow 2a for an initialized *autounsealed* OpenBao instance, or 2b for an uninitialized *autounsealed* OpenBao instance.

### 2a. Migrate the *autounsealed* OpenBao instance to auto-unseal

In this step, the OpenBao instance being migrated needs to be unsealed with the existing *manual unseal keys*, and migrate its data to auto-unseal. To do this, unseal the OpenBao instance with the `-migrate` flag.

```bash
bao operator unseal -migrate ${token}
```

### 2b. If not already initialized, initialize and authorize the *autounsealed* OpenBao instance

Configure your CLI to interact with the *autounsealed* OpenBao instance. See the getting started guide for more information on how to do this. In short, you will need to set the `OPENBAO_ADDR` environment variable to the address of the *autounsealed* OpenBao instance, and retrieve and set the appropriate CA certificate.

```bash
bao operator init
```

Use the root token to create a temporary token, and authorize the OpenBao charm with it.

```console
$ openbao token create -ttl=10m
Key                  Value
---                  -----
token                hvs.mmMXCLNZ2X7OcqCM38WYDnoX
token_accessor       eXzWoD1ajA5YtNgfopj1DP1r
token_duration       10m
token_renewable      true
token_policies       ["root"]
identity_policies    []
policies             ["root"]
```

Create a secret that contains the token above
```console
$ juju add-secret approle_authorization_token token="hvs.mmMXCLNZ2X7OcqCM38WYDnoX"
secret:cqgj49fmp25c7796r0pg
```

Grant the secret to the *autounsealed* openbao, and provide the ID of the secret to the `authorize-charm` action.
```bash
juju grant-secret approle_authorization_token openbao-autounsealed
juju run openbao-autounsealed/leader authorize-charm secret-id=cqgj49fmp25c7796r0pg
```
