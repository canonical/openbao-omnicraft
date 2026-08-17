# Scale (K8s)

The OpenBao charm uses the [raft](https://openbao.org/docs/configuration/storage/raft) backend to scale. This guide walks you through scaling OpenBao.

## Pre-requisites

- OpenBao is initialised and unsealed
- The OpenBao charm is authorised

## 1. Validate that OpenBao is an active state

Run `juju status`:

```
Model  Controller          Cloud/Region        Version  SLA          Timestamp
demo   microk8s-localhost  microk8s/localhost  3.4.0    unsupported  12:52:32-04:00

App    Version  Status   Scale  Charm      Channel    Rev  Address         Exposed  Message
openbao           waiting      1  openbao-k8s  2.0/edge  198  10.152.183.208  no       installing agent

Unit      Workload  Agent  Address      Ports  Message
openbao/0*  active    idle   10.1.182.38
```

## 2. Scale OpenBao to 3 units

Add 2 more units:

```
juju add-unit openbao -n 2
```

The new units will be sealed:

```
Model  Controller          Cloud/Region        Version  SLA          Timestamp
demo   microk8s-localhost  microk8s/localhost  3.4.0    unsupported  12:54:51-04:00

App    Version  Status   Scale  Charm      Channel    Rev  Address         Exposed  Message
openbao           waiting      3  openbao-k8s  2.0/edge  198  10.152.183.208  no       installing agent

Unit      Workload  Agent  Address      Ports  Message
openbao/0*  active    idle   10.1.182.38
openbao/1   blocked   idle   10.1.182.51         Please unseal OpenBao
openbao/2   blocked   idle   10.1.182.34         Please unseal OpenBao
```

Set the `OPENBAO_ADDR` variable to the `openbao/1` unit:

```
export OPENBAO_ADDR=https://$(juju status openbao/1 --format=yaml |  yq -r '.applications.openbao.units.openbao/1.address'):8200; echo $OPENBAO_ADDR
```

Set the `OPENBAO_SKIP_VERIFY` to true:

```
export OPENBAO_SKIP_VERIFY=true
```

Unseal the the `openbao/1` unit using the same unseal keys as received during the initialization of the OpenBao leader:

```
bao operator unseal EJoB62t286mjUpSQYZg3mOla3lz/bbElVL5OLnj+rpE=
```

And complete the same operations for the `openbao/2` unit:

```
export OPENBAO_ADDR=https://$(juju status openbao/2 --format=yaml |  yq -r '.applications.openbao.units.openbao/2.address'):8200; echo $OPENBAO_ADDR
bao operator unseal EJoB62t286mjUpSQYZg3mOla3lz/bbElVL5OLnj+rpE=
```

## 3. Validate that all units are part of the cluster

All units should go to the `Active/Idle` Juju status:

```
$ juju status
Model  Controller          Cloud/Region        Version  SLA          Timestamp
demo   microk8s-localhost  microk8s/localhost  3.4.0    unsupported  12:57:52-04:00

App    Version  Status  Scale  Charm      Channel    Rev  Address         Exposed  Message
openbao           active      3  openbao-k8s  2.0/edge  198  10.152.183.208  no

Unit      Workload  Agent  Address      Ports  Message
openbao/0*  active    idle   10.1.182.38
openbao/1   active    idle   10.1.182.51
openbao/2   active    idle   10.1.182.34
```

And they should all be part of the raft cluster:

```
$ bao operator raft list-peers
Node            Address                                                State       Voter
----            -------                                                -----       -----
demo-openbao/0    openbao-0.openbao-endpoints.demo.svc.cluster.local:8201    leader      true
demo-openbao/1    openbao-1.openbao-endpoints.demo.svc.cluster.local:8201    follower    true
demo-openbao/2    openbao-2.openbao-endpoints.demo.svc.cluster.local:8201    follower    true
```
