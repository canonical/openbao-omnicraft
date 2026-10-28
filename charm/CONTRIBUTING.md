# Contributing

To make contributions to this charm, you'll need a working [development setup](https://juju.is/docs/sdk/dev-setup).

This project uses `uv`. You can install it on Ubuntu with:

```shell
sudo snap install --classic astral-uv
```

You can create an environment for development with `uv`:

```shell
uv sync
source .venv/bin/activate
```

## Testing

This project uses `tox` for managing test environments. It can be installed with:

```shell
uv tool install tox --with tox-uv
```

There are some pre-configured environments that can be used for linting
and formatting code when you're preparing contributions to the charm:

```shell
tox run -e format        # update your code according to linting rules
tox run -e lint          # code style
tox run -e static        # static type checking
tox run -e unit          # unit tests
tox                      # runs 'format', 'lint', 'static', and 'unit' environments
```

### Running the integration tests locally

To run the integration tests locally, you will need to have a Juju controller on a machine substrate active (such as lxd).

First, you need to build the `openbao` charm, as well as the test `openbao-kv-requirer` charm. From the `machine/` directory, run the following commands:

```shell
make copy-test-libs
charmcraft pack
charmcraft pack --project-dir tests/integration/openbao_kv_requirer_operator/
```

The integration tests are run using `tox`. You can run them with:

```shell
tox run -e integration -- --charm_path ./openbao_amd64.charm --kv_requirer_charm_path ./openbao-kv-requirer_amd64.charm -k test_autounseal.py
```

Where the `-k` argument is the test suite you want to run.

Or, to run a specific test:

```shell
tox run -e integration -- --charm_path ./openbao_amd64.charm --kv_requirer_charm_path ./openbao-kv-requirer_amd64.charm -k test_given_openbao_is_deployed_when_integrate_another_openbao_then_autounseal_activated
```

At this time, each integration test suite must be run separately.

#### PKCS#11 SoftHSM tests

`test_pkcs11_hsm.py` prepares SoftHSM on the **test runner** (not the Juju unit):
`snap install softhsm` (optional local `.snap` via `--softhsm-snap-path` /
`OPENBAO_SOFTHSM_SNAP`, or `OPENBAO_SOFTHSM_CHANNEL` for a non-default channel),
create a token + AES key with SoftHSM + OpenSC, and pack `libsofthsm2.so` (+ deps),
`tokens/`, `softhsm2.conf`, and `openbao.env` as `hsm-lib`. The charm unpacks the
resource, installs `openbao.env` for `baod-start`, rewrites the PKCS#11 seal from the
Juju secret, and restarts OpenBao. The suite then verifies PKCS#11 init + restart
auto-unseal.

Pass a local OpenBao snap that ships `plugins/openbao-plugin-kms-pkcs11` with
`--resource-path` when your tox/integration setup requires it.

```shell
tox run -e integration -- \
  --charm_path ./openbao_amd64.charm \
  --kv_requirer_charm_path ./openbao-kv-requirer_amd64.charm \
  -k test_pkcs11_hsm.py
```

#### Backup tests

Machine backup integration tests use MicroCeph RGW (`test_backup_microceph.py`). CI installs MicroCeph, enables RGW on port 7480, and creates the `openbao-microceph-test` bucket. Run that suite locally after matching that setup:

```shell
tox run -e integration -- \
  --charm_path ./openbao_amd64.charm \
  --kv_requirer_charm_path ./openbao-kv-requirer_amd64.charm \
  -k test_backup_microceph.py
```

## Build the charm

Build the charm in this git repository using:

```shell
charmcraft pack
```
