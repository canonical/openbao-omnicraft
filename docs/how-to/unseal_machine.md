# Unseal a sealed unit (Machine)

If an OpenBao unit restarts, you must unseal it. Use the charm `unseal` action rather than calling the OpenBao API directly.

Starting from a cluster where one unit is sealed:

```
$ juju status
Model  Controller           Cloud/Region         Version  SLA          Timestamp
demo   localhost-localhost  localhost/localhost  3.4.0    unsupported  12:34:35-04:00

App    Version  Status   Scale  Charm  Channel    Rev  Exposed  Message
openbao           blocked      3  openbao  1.19/edge  257  no       Please unseal OpenBao (see `unseal` action)

Unit      Workload  Agent  Machine  Public address  Ports  Message
openbao/0*  active    idle   0        10.191.126.116
openbao/1   active    idle   1        10.191.126.151
openbao/2   blocked   idle   2        10.191.126.90          Please unseal OpenBao (see `unseal` action)

Machine  State    Address         Inst id        Base          AZ  Message
0        started  10.191.126.116  juju-b8368f-0  ubuntu@22.04      Running
1        started  10.191.126.151  juju-b8368f-1  ubuntu@22.04      Running
2        started  10.191.126.90   juju-b8368f-2  ubuntu@22.04      Running
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
