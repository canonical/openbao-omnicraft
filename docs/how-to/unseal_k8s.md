# Unseal a sealed unit (K8s)

If an OpenBao unit restarts, you must unseal it. Use the charm `unseal` action rather than calling the OpenBao API directly.

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
openbao/2   blocked   idle   10.1.182.15         Please unseal OpenBao (see `unseal` action)
```

If the initialization secret from `juju run openbao/leader initialize` has not expired, reuse it:

```
juju run openbao/2 unseal secret-id=<secret-id>
```

Otherwise create a Juju secret that contains the unseal key share (the same key you stored offline at initialization):

```
juju add-secret unseal-key key=<unseal-key>
juju grant-secret unseal-key openbao
juju run openbao/2 unseal secret-id=<secret-id>
```

For Shamir with multiple shares, run the action until the threshold is met. Additional shares from the initialize secret are stored as `key-2`, `key-3`, and so on:

```
juju run openbao/2 unseal secret-id=<secret-id> key-name=key-2
```

The unit will go back to the active/idle state.

The OpenBao CLI (`bao operator unseal`) remains available as an advanced fallback. The supported operator path is the charm action.
