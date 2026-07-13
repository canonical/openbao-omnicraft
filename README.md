# OpenBao Omnicraft

This repo contains a monorepo for the Rock, Snap and the Charm for OpenBao.

## Known issues

- **openbao-k8s on juju 4.0.10+**: juju's kubernetes secret backend fails to save
  charm-owned secrets ("cannot patch resource \"secrets\" ... juju-secret-consumer"),
  which prevents the openbao-k8s charm from storing its CA and approle secrets, and
  blocks the k8s integration tests. Tracked upstream as
  [juju/juju#22724](https://github.com/juju/juju/issues/22724) and
  [juju/juju#22485](https://github.com/juju/juju/issues/22485). juju 3.6 (and 4.0.5)
  are unaffected.
