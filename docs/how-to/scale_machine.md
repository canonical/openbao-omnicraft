# Scale (Machine)

The OpenBao charm uses the [raft](https://openbao.org/docs/configuration/storage/raft) backend to scale. This guide walks you through scaling OpenBao.

## Pre-requisites

- OpenBao is initialised and unseal
- The OpenBao charm is authorised

## 1. Validate that OpenBao is an active state

Run `juju status`:
```
Model  Controller           Cloud/Region         Version  SLA          Timestamp
demo   localhost-localhost  localhost/localhost  3.4.0    unsupported  12:11:19-04:00

App    Version  Status  Scale  Charm  Channel    Rev  Exposed  Message
openbao           active      1  openbao  1.19/edge  257  no       

Unit      Workload  Agent  Machine  Public address  Ports  Message
openbao/0*  active    idle   0        10.191.126.116         

Machine  State    Address         Inst id        Base          AZ  Message
0        started  10.191.126.116  juju-b8368f-0  ubuntu@22.04      Running
```

## 2. Scale OpenBao to 3 units

Add 2 more units:

```
juju add-unit openbao -n 2
```

The new units will be sealed:

```
Model  Controller           Cloud/Region         Version  SLA          Timestamp
demo   localhost-localhost  localhost/localhost  3.4.0    unsupported  12:19:14-04:00

App    Version  Status   Scale  Charm  Channel    Rev  Exposed  Message
openbao           blocked      3  openbao  1.19/edge  257  no       Waiting for OpenBao to be unsealed

Unit      Workload  Agent  Machine  Public address  Ports  Message
openbao/0*  active    idle   0        10.191.126.116         
openbao/1   blocked   idle   1        10.191.126.151         Waiting for OpenBao to be unsealed
openbao/2   blocked   idle   2        10.191.126.90          Waiting for OpenBao to be unsealed

Machine  State    Address         Inst id        Base          AZ  Message
0        started  10.191.126.116  juju-b8368f-0  ubuntu@22.04      Running
1        started  10.191.126.151  juju-b8368f-1  ubuntu@22.04      Running
2        started  10.191.126.90   juju-b8368f-2  ubuntu@22.04      Running

```

Set the `BAO_ADDR` variable to the `openbao/1` unit:
```
export BAO_ADDR=https://$(juju status openbao/1 --format=yaml | awk '/public-address/ { print $2 }'):8200; echo $BAO_ADDR
```
Unseal the the `openbao/1` unit using the same unseal keys as received during the initialization of the OpenBao leader:

```
bao operator unseal EJoB62t286mjUpSQYZg3mOla3lz/bbElVL5OLnj+rpE=
```

And complete the same operations for the `openbao/2` unit:

```
export BAO_ADDR=https://$(juju status openbao/2 --format=yaml | awk '/public-address/ { print $2 }'):8200; echo $BAO_ADDR
bao operator unseal EJoB62t286mjUpSQYZg3mOla3lz/bbElVL5OLnj+rpE=
```

## 3. Validate that all units are part of the cluster

All units should go to the `Active/Idle` Juju status:

```
$ juju status
Model  Controller           Cloud/Region         Version  SLA          Timestamp
demo   localhost-localhost  localhost/localhost  3.4.0    unsupported  12:24:32-04:00

App    Version  Status  Scale  Charm  Channel    Rev  Exposed  Message
openbao           active      3  openbao  1.19/edge  257  no       

Unit      Workload  Agent  Machine  Public address  Ports  Message
openbao/0*  active    idle   0        10.191.126.116         
openbao/1   active    idle   1        10.191.126.151         
openbao/2   active    idle   2        10.191.126.90          

Machine  State    Address         Inst id        Base          AZ  Message
0        started  10.191.126.116  juju-b8368f-0  ubuntu@22.04      Running
1        started  10.191.126.151  juju-b8368f-1  ubuntu@22.04      Running
2        started  10.191.126.90   juju-b8368f-2  ubuntu@22.04      Running

```

And they should all be part of the raft cluster:

```
$ bao operator raft list-peers
Node            Address                State       Voter
----            -------                -----       -----
demo-openbao/0    10.191.126.116:8201    leader      true
demo-openbao/1    10.191.126.151:8201    follower    true
demo-openbao/2    10.191.126.90:8201     follower    true
```
