# Unseal a sealed unit (K8s)

In the circumstance that a OpenBao unit restarts, you will have to manually unseal it. This guide walks you through the necessary steps:

Starting from a cluster where one unit is sealed:

```
$ juju status
Model  Controller          Cloud/Region        Version  SLA          Timestamp
demo   microk8s-localhost  microk8s/localhost  3.4.0    unsupported  13:02:12-04:00

App    Version  Status   Scale  Charm      Channel    Rev  Address         Exposed  Message
openbao           waiting      3  openbao-k8s  2.0/edge  198  10.152.183.208  no       installing agent

Unit      Workload  Agent  Address      Ports  Message
openbao/0*  active    idle   10.1.182.38
openbao/1   active    idle   10.1.182.51
openbao/2   blocked   idle   10.1.182.15         Please unseal OpenBao
```

Set the `OPENBAO_ADDR` variable to the sealed unit:

```
export OPENBAO_ADDR=https://$(juju status openbao/2 --format=yaml |  yq -r '.applications.openbao.units.openbao/2.address'):8200; echo $OPENBAO_ADDR
```

Unseal the the unit using the same unseal keys as received during the initialization of the OpenBao leader:

```
bao operator unseal -tls-skip-verify EJoB62t286mjUpSQYZg3mOla3lz/bbElVL5OLnj+rpE=
```

The units will go back to the active/idle state:

```
$ juju status
Model  Controller          Cloud/Region        Version  SLA          Timestamp
demo   microk8s-localhost  microk8s/localhost  3.4.0    unsupported  13:03:26-04:00

App    Version  Status  Scale  Charm      Channel    Rev  Address         Exposed  Message
openbao           active      3  openbao-k8s  2.0/edge  198  10.152.183.208  no

Unit      Workload  Agent  Address      Ports  Message
openbao/0*  active    idle   10.1.182.38
openbao/1   active    idle   10.1.182.51
openbao/2   active    idle   10.1.182.15
```
