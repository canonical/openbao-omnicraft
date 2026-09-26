# Getting Started (Machine)

In this tutorial, we will deploy OpenBao on an LXD cloud.

## Pre-requisites
A Ubuntu 26.04 machine with the following requirements:

* A `x86_64` CPU
* 8GB of RAM
* 20GB of free disk space

## 1. Install LXD

```shell
sudo snap install lxd
```

## 2. Bootstrap a Juju controller

Bootstrap a LXD Juju controller:

```shell
juju bootstrap localhost localhost
```

## 3. Deploy OpenBao

Create a Juju model named `demo`:

```shell
juju add-model demo
```

Deploy the OpenBao operator:

```shell
juju deploy openbao --channel=2/edge
```

Deploying OpenBao will take several minutes, wait for the unit to be in the `blocked/idle` state, awaiting initialisation.

```shell
$ juju status
Model  Controller           Cloud/Region         Version  SLA          Timestamp
demo   localhost-localhost  localhost/localhost  3.6.28    unsupported  11:41:15-04:00

App    Version  Status   Scale  Charm  Channel    Rev  Exposed  Message
openbao           blocked      1  openbao  2/edge  2  no       Please initialize OpenBao or integrate with an auto-unseal provider

Unit      Workload  Agent  Machine  Public address  Ports  Message
openbao/0*  blocked   idle   0        10.102.71.106          Please initialize OpenBao or integrate with an auto-unseal provider

Machine  State    Address        Inst id        Base          AZ  Message
0        started  10.102.71.106  juju-c3e914-0  ubuntu@26.04      Running
```

## 4. Set up the OpenBao CLI

To communicate with OpenBao via CLI, we need to install the OpenBao CLI client and set the following environment variables:
* `BAO_ADDR`
* `BAO_TOKEN`
* `BAO_CAPATH`

Install the [OpenBao client](https://snapcraft.io/openbao) and [yq](https://snapcraft.io/yq):

```shell
sudo snap install openbao
sudo snap install yq
```

Set the `BAO_ADDR` environment variable:

```shell
export BAO_ADDR=https://$(juju status openbao/leader --format=yaml | awk '/public-address/ { print $2 }' | head -n 1):8200; echo $BAO_ADDR
```

Extract and store OpenBao's CA certificate to a `openbao.pem` file:

```shell
cert_juju_secret_id=$(juju secrets --format=yaml | yq -r 'to_entries | .[] | select(.value.label == "self-signed-openbao-ca-certificate") | .key'); echo $cert_juju_secret_id
juju show-secret ${cert_juju_secret_id} --reveal --format=yaml | yq -r '.[].content.certificate' > openbao.pem
```

This will put the CA certificate in a file called `openbao.pem`. Now, you can point the `openbao` client to this file by setting the `BAO_CAPATH` variable.

```shell
export BAO_CAPATH=$(pwd)/openbao.pem; echo $BAO_CAPATH
```

Validate that OpenBao is accessible and up and running:

```shell
bao status
```

You should expect the following output.

```shell
$ bao status
Key                Value
---                -----
Seal Type          shamir
Initialized        false
Sealed             true
Total Shares       0
Threshold          0
Unseal Progress    0/0
Unseal Nonce       n/a
Version            2.6.0
Commit Date        2026-07-14T11:34:01Z
Storage Type       raft
HA Enabled         true
```

## 5. Initialise and unseal OpenBao

Initialise OpenBao:

```shell
$ bao operator init -key-shares=5 -key-threshold=3
Unseal Key 1: O0s4NqdQnLVXl2V+R1fEodgWDFuz1sAvp6jQOyG8UAds
Unseal Key 2: JTyIp83EfcqQLibHv7MjFivjVsol/m+gvbLRFtcKXxkM
Unseal Key 3: H0JTnCtsqL4jXN2XI6jcWa+pP5RAoyApLzezhUoISL+3
Unseal Key 4: pA8kIIue7JpVfln3MnVCotc98LpP4F49EYCWX/4ae8rl
Unseal Key 5: LSuRCSrC1K0FZdGUIB0n24hUbR+p4jpmYsfFDd6tqogu

Initial Root Token: s.4QI7iEuOefUXy9qHp32fLDZB

Vault initialized with 5 key shares and a key threshold of 3. Please securely
distribute the key shares printed above. When the Vault is re-sealed,
restarted, or stopped, you must supply at least 3 of these keys to unseal it
before it can start servicing requests.

Vault does not store the generated root key. Without at least 3 keys to
reconstruct the root key, Vault will remain permanently sealed!

It is possible to generate new unseal keys, provided you have a quorum
of existing unseal keys shares. See "bao operator rotate-keys" for more
information.
```

Set the `BAO_TOKEN` variable using the root token:
```
export BAO_TOKEN=s.4QI7iEuOefUXy9qHp32fLDZB
```

Unseal OpenBao using the unseal key:

```shell
bao operator unseal O0s4NqdQnLVXl2V+R1fEodgWDFuz1sAvp6jQOyG8UAds
bao operator unseal JTyIp83EfcqQLibHv7MjFivjVsol/m+gvbLRFtcKXxkM
bao operator unseal H0JTnCtsqL4jXN2XI6jcWa+pP5RAoyApLzezhUoISL+3
```

## 6. Authorise the OpenBao charm

Create a token:

```
$ bao token create -ttl=10m
Key                  Value
---                  -----
token                s.UdFZa2Gv4kKoHpYWSnNnYOZr
token_accessor       kKFFEWKFe5FDRdIxpP4Yz7mm
token_duration       10m
token_renewable      true
token_policies       ["root"]
identity_policies    []
policies             ["root"]
```

Add the token as a juju user secret

```shell
juju add-secret one-time-token token=s.UdFZa2Gv4kKoHpYWSnNnYOZr
```

Grant this secret to the charm

```shell
juju grant-secret one-time-token openbao
```

Authorise the charm to interact with OpenBao using the token value from the secret:

```shell
juju run openbao/leader authorize-charm secret-id="f52b45t3fkqpndb8o44g"
```

You may now remove the secret

```shell
juju remove-secret one-time-token
```

## 7. Create a key-value type secret

Enable the `kv` secret engine:

```
bao secrets enable -version=2 kv
```

Create a secret under the `kv/mypasswords` path with these attributes:

* key: `bob`
* value: `1jioaf123901jdeja`

```
bao kv put kv/mypasswords bob=1jioaf123901jdeja
```

Good job, you created your first secret!

You can now retrieve it:

```
bao kv get kv/mypasswords
```

And delete it:

```
bao kv delete kv/mypasswords
```

## 8. Destroy the environment

Destroy the Juju controller and its models:

```
juju kill-controller localhost-localhost
```

Uninstall all the installed packages:

```
sudo snap remove juju --purge
sudo snap remove yq --purge
sudo snap remove openbao --purge
```
