# Unseal a sealed unit (Machine)

In the circumstance that a OpenBao unit restarts, you will have to manually unseal it. This guide walks you through the necessary steps:

Starting from a cluster where one unit is sealed:
```
$ juju status
Model  Controller           Cloud/Region         Version  SLA          Timestamp
demo   localhost-localhost  localhost/localhost  3.4.0    unsupported  12:34:35-04:00

App    Version  Status   Scale  Charm  Channel    Rev  Exposed  Message
openbao           blocked      3  openbao  1.19/edge  257  no       Waiting for OpenBao to be unsealed

Unit      Workload  Agent  Machine  Public address  Ports  Message
openbao/0*  active    idle   0        10.191.126.116         
openbao/1   active    idle   1        10.191.126.151         
openbao/2   blocked   idle   2        10.191.126.90          Waiting for OpenBao to be unsealed

Machine  State    Address         Inst id        Base          AZ  Message
0        started  10.191.126.116  juju-b8368f-0  ubuntu@22.04      Running
1        started  10.191.126.151  juju-b8368f-1  ubuntu@22.04      Running
2        started  10.191.126.90   juju-b8368f-2  ubuntu@22.04      Running
```

Set the `OPENBAO_ADDR` variable to the sealed unit:

```
export OPENBAO_ADDR=https://$(juju status openbao/2 --format=yaml | awk '/public-address/ { print $2 }'):8200; echo $OPENBAO_ADDR
```

Unseal the the unit using the same unseal keys as received during the initialization of the OpenBao leader:

```
bao operator unseal EJoB62t286mjUpSQYZg3mOla3lz/bbElVL5OLnj+rpE=
```

The units will go back to the active/idle state:

```
$ juju status
demo   localhost-localhost  localhost/localhost  3.4.0    unsupported  12:39:11-04:00

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
