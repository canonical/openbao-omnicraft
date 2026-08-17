# Getting Started (Kubernetes)

In this tutorial, we will deploy OpenBao on Kubernetes and use it to store a very important secret.

## Pre-requisites

A Ubuntu 22.04 machine with the following requirements:

- A `x86_64` CPU
- 8GB of RAM
- 20GB of free disk space

## 1. Install MicroK8s

```shell
sudo snap install microk8s --channel=1.29-strict/stable
```

Enable the storage and dns addons:

```
sudo microk8s enable hostpath-storage
sudo microk8s enable dns
```

## 2. Bootstrap a Juju controller

From your terminal, install Juju:

```
sudo snap install juju --channel=3.6/stable
```

Bootstrap a Juju controller:

```
juju bootstrap microk8s
```

## 3. Deploy OpenBao

Create a Juju model named `demo`:

```shell
juju add-model demo
```

Deploy the OpenBao K8s operator:

```shell
juju deploy openbao-k8s openbao --channel=2.0/edge --trust
```

```{tip}
The charm declares minimum storage sizes (e.g. 10G for Raft data). You can provision
larger volumes at deploy time using `--storage openbao-raft=50G`. Storage sizes cannot
be changed after deployment on Kubernetes. See the
[Production blueprint](../reference/production_blueprint_k8s.md) for details.
```

Deploying OpenBao will take several minutes, wait for the unit to be in the `blocked/idle` state, awaiting initialisation.

```shell
$ juju status
Model  Controller          Cloud/Region        Version  SLA          Timestamp
demo   microk8s-localhost  microk8s/localhost  3.6.8    unsupported  12:31:45-04:00

App    Version  Status   Scale  Charm      Channel    Rev  Address         Exposed  Message
openbao           blocked      1  openbao-k8s  2.0/edge  380  10.152.183.183  no       Please initialize OpenBao or integrate with an auto-unseal provider

Unit      Workload  Agent  Address     Ports  Message
openbao/0*  blocked   idle   10.1.0.237         Please initialize OpenBao or integrate with an auto-unseal provider
```

## 4. Set up the OpenBao CLI

To communicate with OpenBao via CLI, we need to install the OpenBao CLI client and set the following environment variables:

- `OPENBAO_ADDR`
- `BAO_TOKEN`
- `OPENBAO_CAPATH`

Install the [OpenBao client](https://snapcraft.io/openbao) and [yq](https://snapcraft.io/yq):

```shell
sudo snap install openbao
sudo snap install yq
```

Set the `OPENBAO_ADDR` environment variable:

```shell
export OPENBAO_ADDR=https://$(juju status openbao/leader --format=yaml | yq -r '.applications.openbao.address'):8200; echo $OPENBAO_ADDR
```

Extract and store OpenBao's CA certificate to a `openbao.pem` file:

```shell
cert_juju_secret_id=$(juju secrets --format=yaml | yq -r 'to_entries | .[] | select(.value.label == "self-signed-openbao-ca-certificate") | .key'); echo $cert_juju_secret_id
juju show-secret ${cert_juju_secret_id} --reveal --format=yaml | yq -r '.[].content.certificate' > openbao.pem
```

This will put the CA certificate in a file called `openbao.pem`. Now, you can point the `openbao` client to this file by setting the `OPENBAO_CAPATH` variable.

```shell
export OPENBAO_CAPATH=$(pwd)/openbao.pem; echo $OPENBAO_CAPATH
```

Validate that OpenBao is accessible and up and running:

```shell
openbao status
```

You should expect the following output.

```shell
$ openbao status
Key                Value
---                -----
Seal Type          shamir
Initialized        false
Sealed             true
Total Shares       0
Threshold          0
Unseal Progress    0/0
Unseal Nonce       n/a
Version            1.19.5
Build Date         2024-07-10T15:37:35Z
Storage Type       raft
HA Enabled         true
```

## 5. Initialise and unseal OpenBao

Initialise OpenBao:

```shell
$ bao operator init -key-shares=1 -key-threshold=1
Unseal Key 1: NXw7vSzWOnNuNF2v5aEkQcQy/TdTuryYS9Qz3hxDS38=

Initial Root Token: hvs.0d26h3eSnlZzpUoVu49Sj64V

OpenBao initialized with 1 key shares and a key threshold of 1. Please securely
distribute the key shares printed above. When the OpenBao is re-sealed,
restarted, or stopped, you must supply at least 1 of these keys to unseal it
before it can start servicing requests.

OpenBao does not store the generated root key. Without at least 1 keys to
reconstruct the root key, OpenBao will remain permanently sealed!

It is possible to generate new unseal keys, provided you have a quorum of
existing unseal keys shares. See "bao operator rekey" for more information.
```

Set the `BAO_TOKEN` variable using the root token:

```
export BAO_TOKEN=hvs.0d26h3eSnlZzpUoVu49Sj64V
```

Unseal OpenBao using the unseal key:

```shell
bao operator unseal NXw7vSzWOnNuNF2v5aEkQcQy/TdTuryYS9Qz3hxDS38=
```

## 6. Authorise the OpenBao charm

Create a token:

```
$openbao token create -ttl=10m
Key                  Value
---                  -----
token                hvs.M9vfjsKfv1zOgU6QTuFJblwP
token_accessor       ctfCqC3MX8vGH9G7Z3URgWsR
token_duration       10m
token_renewable      true
token_policies       ["root"]
identity_policies    []
policies             ["root"]
```

Add the token as a juju user secret

```shell
juju add-secret one-time-token token=hvs.0d26h3eSnlZzpUoVu49Sj64V
```

Grant this secret to the charm

```shell
juju grant-secret one-time-token openbao
```

Authorise the charm to interact with OpenBao using the token value from the secret:

```shell
juju run openbao/leader authorize-charm secret-id="cq3rldnmp25c7bvnhim0"
```

You may now remove the secret

```shell
juju remove-secret one-time-token
```

## 7. Create a key-value type secret

Enable the `kv` secret engine:

```
openbao secrets enable -version=2 kv
```

Create a secret under the `kv/mypasswords` path with these attributes:

- key: `bob`
- value: `1jioaf123901jdeja`

```shell
openbao kv put kv/mypasswords bob=1jioaf123901jdeja
```

Good job, you created your first secret!

You can now retrieve it:

```shell
openbao kv get kv/mypasswords
```

And delete it:

```shell
openbao kv delete kv/mypasswords
```

## 8. Destroy the environment

Destroy the Juju controller and its models:

```shell
juju kill-controller microk8s-localhost
```

Uninstall all the installed packages:

```shell
sudo snap remove juju --purge
sudo snap remove yq --purge
sudo snap remove openbao --purge
```
