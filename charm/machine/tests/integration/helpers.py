#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import contextlib
import json
import logging
import os
import platform
import tempfile
import time
from pathlib import Path
from typing import Any, List, Tuple

import jubilant
from cryptography import x509

import config
from config import (
    APP_NAME,
    JUJU_FAST_INTERVAL,
    NUM_OPENBAO_UNITS,
    OPENBAO_PKI_REQUIRER_APPLICATION_NAME,
    S3_INTEGRATOR_APPLICATION_NAME,
    SELF_SIGNED_CERTIFICATES_APPLICATION_NAME,
    SHORT_TIMEOUT,
)
from openbao_helpers import OpenBao

logger = logging.getLogger(__name__)


class ActionFailedError(Exception):
    """Exception raised when an action fails."""

    pass


@contextlib.contextmanager
def fast_forward(juju: jubilant.Juju, fast_interval: str = JUJU_FAST_INTERVAL):
    juju.model_config({"update-status-hook-interval": fast_interval})
    try:
        yield
    finally:
        juju.model_config(reset="update-status-hook-interval")


def scale(juju: jubilant.Juju, app_name: str, target: int) -> None:
    status = juju.status()
    current = len(status.apps[app_name].units)
    if current < target:
        juju.add_unit(app_name, num_units=target - current)
    elif current > target:
        juju.remove_unit(app_name, num_units=current - target)


def get_leader_unit_name(juju: jubilant.Juju, app_name: str) -> str:
    """Return the leader unit name for the given application."""
    status = juju.status()
    for unit_name, unit in status.apps[app_name].units.items():
        if unit.leader:
            return unit_name
    raise RuntimeError(f"Leader unit for `{app_name}` not found.")


def get_unit_address(juju: jubilant.Juju, unit_name: str) -> str:
    """Return the address of the given unit."""
    app_name = unit_name.split("/")[0]
    status = juju.status()
    return status.apps[app_name].units[unit_name].public_address


def get_first(d: dict) -> Any:
    return next(iter(d.values()))


def has_relation(juju: jubilant.Juju, app_name: str, relation_name: str) -> bool:
    """Check if the application has the relation with the given name."""
    status = juju.status()
    if app_name not in status.apps:
        return False
    return relation_name in status.apps[app_name].relations


def get_ca_cert_file_location(juju: jubilant.Juju, app_name: str = APP_NAME) -> str | None:
    """Get the location of the CA certificate file."""
    if not has_relation(juju, app_name, "tls-certificates-access"):
        return None
    action_output = run_get_ca_certificate_action(juju)
    ca_certificate = action_output["ca-certificate"]
    assert ca_certificate
    ca_file_location = os.path.join(tempfile.gettempdir(), f"ca_file_{app_name}.txt")
    with open(ca_file_location, mode="w+") as ca_file:
        ca_file.write(ca_certificate)
    return ca_file_location


def run_get_ca_certificate_action(juju: jubilant.Juju, timeout: int = 60) -> dict:
    """Run the `get-ca-certificate` on the `self-signed-certificates` unit."""
    return juju.run(
        f"{SELF_SIGNED_CERTIFICATES_APPLICATION_NAME}/0",
        "get-ca-certificate",
        {},
        wait=timeout,
    ).results


def authorize_charm(juju: jubilant.Juju, root_token: str, app_name: str = APP_NAME) -> Any:
    """Authorize the charm to interact with OpenBao."""
    status = juju.status()
    if jubilant.all_active(status, app_name):
        logger.info("The charm is already active, skipping authorization.")
        return
    logger.info("Authorizing the charm `%s` to interact with OpenBao.", app_name)
    secret_name = f"approle-token-{app_name}"
    secret_uri = juju.add_secret(secret_name, {"token": root_token})
    secret_id = secret_uri.split(":")[-1]
    juju.grant_secret(secret_name, app_name)
    return run_action_on_leader(juju, app_name, "authorize-charm", secret_id=secret_id)


def authorize_charm_and_wait(
    juju: jubilant.Juju, root_token: str, app_name: str = APP_NAME
) -> Any:
    """Authorize the charm and wait for it to be authorized."""
    result = authorize_charm(juju, root_token, app_name)
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, app_name),
            timeout=60,
            error=None,
        )
    logger.info("Charm authorized")
    return result


def get_openbao_token_and_unseal_key(
    juju: jubilant.Juju, app_name: str = APP_NAME
) -> Tuple[str, str]:
    root_token, unseal_key = get_juju_secret(
        juju, label=f"root-token-key-{app_name}", fields=["root-token", "key"]
    )
    return root_token, unseal_key


def initialize_openbao_leader(juju: jubilant.Juju, app_name: str) -> Tuple[str, str]:
    """Initialize the leader openbao unit and return the root token and unseal key."""
    leader_name = get_leader_unit_name(juju, app_name)
    openbao = get_openbao_client(juju, leader_name)
    if not openbao.is_initialized():
        root_token, key = openbao.initialize()
        juju.add_secret(
            f"root-token-key-{app_name}",
            {"root-token": root_token, "key": key},
        )
        return root_token, key
    root_token, key = get_openbao_token_and_unseal_key(juju, app_name)
    return root_token, key


def get_openbao_client(
    juju: jubilant.Juju,
    unit_name: str,
    token: str | None = None,
    ca_file_name: str | None = None,
) -> OpenBao:
    """Get a OpenBao client for the given unit."""
    app_name = unit_name.split("/")[0]
    address = juju.status().apps[app_name].units[unit_name].public_address
    return OpenBao(url=f"https://{address}:8200", token=token, ca_file_location=ca_file_name)


def unseal_all_openbao_units(
    juju: jubilant.Juju, unseal_key: str, ca_file_name: str | None = None
) -> None:
    """Unseal all the openbao units."""
    status = juju.status()
    app = status.apps[APP_NAME]

    # We need to unseal the leader first, since this is the one we initialized.
    leader_name = get_leader_unit_name(juju, APP_NAME)
    unit_address = app.units[leader_name].public_address
    assert unit_address
    openbao = OpenBao(url=f"https://{unit_address}:8200")
    if openbao.is_sealed():
        openbao.unseal(unseal_key)
    openbao.wait_for_node_to_be_unsealed()

    for unit_name, unit in app.units.items():
        unit_address = unit.public_address
        assert unit_address
        openbao = OpenBao(url=f"https://{unit_address}:8200", ca_file_location=ca_file_name)
        openbao.unseal(unseal_key)
        openbao.wait_for_node_to_be_unsealed()


def initialize_unseal_authorize_openbao(juju: jubilant.Juju, app_name: str) -> tuple[str, str]:
    root_token, unseal_key = initialize_openbao_leader(juju, app_name)
    leader_name = get_leader_unit_name(juju, app_name)
    openbao = get_openbao_client(juju, leader_name, root_token)
    assert openbao.is_sealed()

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        unseal_all_openbao_units(juju, unseal_key)
        authorize_charm_and_wait(juju, root_token)
    return root_token, unseal_key


def run_get_certificate_action(juju: jubilant.Juju) -> dict:
    """Run `get-certificate` on the `tls-requirer-requirer/0` unit."""
    return juju.run(
        f"{OPENBAO_PKI_REQUIRER_APPLICATION_NAME}/0",
        "get-certificate",
        {},
        wait=30,
    ).results


def wait_for_certificate_to_be_provided(juju: jubilant.Juju) -> None:
    start_time = time.time()
    timeout = 300
    while time.time() - start_time < timeout:
        try:
            action_output = run_get_certificate_action(juju)
        except jubilant.TaskError:
            time.sleep(10)
            continue
        if action_output.get("certificate", None) is not None:
            return
        time.sleep(10)
    raise TimeoutError("Timed out waiting for certificate to be provided.")


def wait_for_status_message(
    juju: jubilant.Juju,
    expected_message: str,
    app_name: str = APP_NAME,
    count: int = 1,
    timeout: int = 100,
    cadence: int = 2,
) -> None:
    """Wait for the correct status messages to appear.

    Args:
        juju: Jubilant Juju instance
        app_name: Application name of the OpenBao, defaults to APP_NAME
        count: How many units are expected to be emitting the message
        expected_message: The message that openbao units should be setting as a status message
        timeout: Wait time, in seconds, before giving up
        cadence: How often to check the status of the units
    """

    def ready(status: jubilant.Status) -> bool:
        if app_name not in status.apps:
            return False
        units = status.apps[app_name].units
        seen = sum(1 for u in units.values() if u.workload_status.message == expected_message)
        return seen == count

    juju.wait(ready, timeout=timeout, delay=cadence)


def deploy_openbao(
    juju: jubilant.Juju,
    num_openbaos: int,
    channel: str | None = None,
    charm_path: Path | None = None,
    revision: int | None = None,
) -> None:
    """Ensure the OpenBao charm is deployed."""
    deploy_if_not_exists(
        juju,
        app_name=APP_NAME,
        charm_path=charm_path,
        num_units=num_openbaos,
        channel=channel,
        revision=revision,
        constraints={"arch": _get_arch()},
    )


def deploy_openbao_and_wait(
    juju: jubilant.Juju,
    num_units: int,
    status: str | None = None,
    channel: str | None = None,
    charm_path: Path | None = None,
    revision: int | None = None,
) -> None:
    deploy_openbao(
        juju, num_openbaos=num_units, channel=channel, charm_path=charm_path, revision=revision
    )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        if status == "blocked":
            juju.wait(
                lambda s: (
                    APP_NAME in s.apps
                    and jubilant.all_blocked(s, APP_NAME)
                    and len(s.apps[APP_NAME].units) >= num_units
                ),
                timeout=1000,
            )
        elif status == "active":
            juju.wait(
                lambda s: (
                    APP_NAME in s.apps
                    and jubilant.all_active(s, APP_NAME)
                    and len(s.apps[APP_NAME].units) >= num_units
                ),
                timeout=1000,
            )
        else:
            juju.wait(
                lambda s: APP_NAME in s.apps and len(s.apps[APP_NAME].units) >= num_units,
                timeout=1000,
            )


def get_leader_unit_address(juju: jubilant.Juju, app_name: str = APP_NAME) -> str:
    leader_name = get_leader_unit_name(juju, app_name)
    address = juju.status().apps[app_name].units[leader_name].public_address
    assert address
    return address


def _get_arch() -> str:
    """Return the Juju architecture name for the current machine."""
    arch_map = {"x86_64": "amd64", "aarch64": "arm64", "s390x": "s390x"}
    return arch_map.get(platform.machine(), "amd64")


def _get_arch_constraint() -> str:
    """Return arch constraint matching the current machine architecture."""
    return f"arch={_get_arch()}"


def _hsm_lib_placeholder_path() -> Path:
    """Return a non-module placeholder tarball so Juju can attach hsm-lib at deploy time.

    Filename must end in ``.gz`` to match the charm resource ``filename: hsm-lib.tar.gz``.
    """
    return Path(__file__).resolve().parents[2] / "hsm-lib-placeholder.tar.gz"


def openbao_charm_resources(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Resources required to deploy the OpenBao charm from a local .charm file."""
    resources = {"hsm-lib": str(_hsm_lib_placeholder_path())}
    if extra:
        resources.update(extra)
    return resources


# SoftHSM defaults used by PKCS#11 integration tests.
SOFTHSM_SNAP_NAME = "softhsm"
# Snap app name (local/devel SoftHSM snap); not published to the store yet.
SOFTHSM_UTIL = "softhsm.softhsm2-util"
SOFTHSM_TOKEN_LABEL = "OpenBao"
SOFTHSM_PIN = "1234"
SOFTHSM_SO_PIN = "1234"
SOFTHSM_KEY_LABEL = "bao-root-key-aes"
SOFTHSM_MODULE_NAME = "libsofthsm2.so"
SOFTHSM_OPENBAO_DIR = "/var/snap/openbao/common/softhsm"
SOFTHSM_CONF_PATH = f"{SOFTHSM_OPENBAO_DIR}/softhsm2.conf"
SOFTHSM_TOKENS_DIR = f"{SOFTHSM_OPENBAO_DIR}/tokens"
OPENBAO_ENV_PATH = "/var/snap/openbao/common/openbao.env"
_REMOTE_SOFTHSM_SNAP = "/tmp/openbao-softhsm.snap"


def _unit_exec(juju: jubilant.Juju, unit_name: str, command: str, *args: str) -> str:
    r"""Run a shell command on a unit and return stdout.

    Prefer ``_unit_exec(juju, unit, "bash", "-lc", script)`` for multiline scripts so
    newlines are preserved as a separate argv element (``json.dumps`` + ``bash -lc``
    leaves literal ``\n`` and breaks heredocs).
    """
    result = juju.exec(command, *args, unit=unit_name)
    return (result.stdout or "").strip()


def _ensure_softhsm_snap_installed(juju: jubilant.Juju, unit_name: str) -> None:
    """Install the SoftHSM snap on the unit if missing (store, else local --dangerous)."""
    already = _unit_exec(
        juju,
        unit_name,
        "bash",
        "-lc",
        f"snap list '{SOFTHSM_SNAP_NAME}' >/dev/null 2>&1 && echo yes || echo no",
    )
    if already == "yes":
        return

    # Prefer the Snap Store when the snap is published.
    try:
        _unit_exec(
            juju,
            unit_name,
            "bash",
            "-lc",
            f"sudo snap install '{SOFTHSM_SNAP_NAME}'",
        )
        return
    except jubilant.TaskError as store_err:
        logger.info(
            "SoftHSM snap not installable from the store on %s (%s); trying local snap",
            unit_name,
            store_err,
        )

    if not config.SOFTHSM_SNAP_PATH:
        raise RuntimeError(
            "SoftHSM snap is not in the Snap Store. Pass --softhsm-snap-path "
            "/path/to/softhsm_*.snap (or set OPENBAO_SOFTHSM_SNAP)."
        )
    juju.cli("scp", config.SOFTHSM_SNAP_PATH, f"{unit_name}:{_REMOTE_SOFTHSM_SNAP}")
    _unit_exec(
        juju,
        unit_name,
        "bash",
        "-lc",
        f"sudo snap install --dangerous '{_REMOTE_SOFTHSM_SNAP}'",
    )


def setup_softhsm_on_unit(juju: jubilant.Juju, unit_name: str) -> dict[str, str]:
    """Install the SoftHSM snap (idempotent), create a token+AES key under snap-common.

    Token storage and ``SOFTHSM2_CONF`` live under ``/var/snap/openbao/common`` so the
    strictly confined OpenBao snap can use them. SoftHSM tools are invoked via the
    snap's staged binaries (not the confined snap app wrappers) so they can write
    under OpenBao's snap-common. Returns Juju secret field content for the PKCS#11
    seal (including ``lib`` = ``libsofthsm2.so``).
    """
    _ensure_softhsm_snap_installed(juju, unit_name)

    # Run SoftHSMv2 binaries from the snap mount (bypass strict app confinement).
    # The util dlopens /usr/lib/softhsm/libsofthsm2.so (snap layout path), so symlink it.
    script = f"""
set -euo pipefail
SNAP_ROOT="$(readlink -f /snap/{SOFTHSM_SNAP_NAME}/current)"
LIBDIR="$(find "$SNAP_ROOT/usr/lib" -maxdepth 1 -type d -name '*-linux-gnu' | head -n1 || true)"
export LD_LIBRARY_PATH="$SNAP_ROOT/usr/lib${{LIBDIR:+:$LIBDIR}}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"
UTIL="$SNAP_ROOT/usr/bin/softhsm2-util"
MODULE="$(find "$SNAP_ROOT" -name '{SOFTHSM_MODULE_NAME}' 2>/dev/null | head -n1)"
test -x "$UTIL"
test -n "$MODULE"
sudo mkdir -p /usr/lib/softhsm
sudo ln -sfn "$MODULE" /usr/lib/softhsm/{SOFTHSM_MODULE_NAME}

sudo mkdir -p '{SOFTHSM_TOKENS_DIR}'
sudo tee '{SOFTHSM_CONF_PATH}' >/dev/null <<'EOF'
directories.tokendir = {SOFTHSM_TOKENS_DIR}
objectstore.backend = file
log.level = INFO
EOF
sudo chmod -R a+rX '{SOFTHSM_OPENBAO_DIR}'

export SOFTHSM2_CONF='{SOFTHSM_CONF_PATH}'
# Re-init cleanly for repeatable test runs.
sudo find '{SOFTHSM_TOKENS_DIR}' -mindepth 1 -delete
sudo -E "$UTIL" --init-token --free \
  --label '{SOFTHSM_TOKEN_LABEL}' \
  --pin '{SOFTHSM_PIN}' \
  --so-pin '{SOFTHSM_SO_PIN}' >/dev/null

# Prefer a pkcs11-tool shipped by the SoftHSM snap; fall back to OpenSC.
if command -v softhsm.pkcs11-tool >/dev/null 2>&1; then
  PKCS11_TOOL=softhsm.pkcs11-tool
elif command -v pkcs11-tool >/dev/null 2>&1; then
  PKCS11_TOOL=pkcs11-tool
else
  export DEBIAN_FRONTEND=noninteractive
  sudo apt-get update -qq
  sudo apt-get install -y -qq opensc
  PKCS11_TOOL=pkcs11-tool
fi
sudo -E "$PKCS11_TOOL" --module "$MODULE" \
  --token-label '{SOFTHSM_TOKEN_LABEL}' \
  --login --pin '{SOFTHSM_PIN}' \
  --keygen --key-type aes:32 \
  --label '{SOFTHSM_KEY_LABEL}' --id 01 >/dev/null

# Ensure the OpenBao snap daemon sees SoftHSM config.
sudo touch '{OPENBAO_ENV_PATH}'
if ! sudo grep -q '^export SOFTHSM2_CONF=' '{OPENBAO_ENV_PATH}'; then
  echo "export SOFTHSM2_CONF={SOFTHSM_CONF_PATH}" | sudo tee -a '{OPENBAO_ENV_PATH}' >/dev/null
else
  sudo sed -i 's|^export SOFTHSM2_CONF=.*|export SOFTHSM2_CONF={SOFTHSM_CONF_PATH}|' '{OPENBAO_ENV_PATH}'
fi

echo "$MODULE"
"""
    module_path = _unit_exec(juju, unit_name, "bash", "-lc", script)
    if not module_path.endswith(SOFTHSM_MODULE_NAME):
        lines = [line for line in module_path.splitlines() if line.strip()]
        module_path = lines[-1] if lines else ""
    if SOFTHSM_MODULE_NAME not in module_path:
        raise RuntimeError(
            f"SoftHSM setup did not report {SOFTHSM_MODULE_NAME} path: {module_path!r}"
        )
    logger.info("SoftHSM ready on %s (module %s)", unit_name, module_path)
    return {
        "pin": SOFTHSM_PIN,
        "token-label": SOFTHSM_TOKEN_LABEL,
        "key-label": SOFTHSM_KEY_LABEL,
        "lib": SOFTHSM_MODULE_NAME,
    }


def build_softhsm_hsm_lib_tarball(juju: jubilant.Juju, unit_name: str, dest: Path) -> Path:
    """Pack SoftHSM's PKCS#11 module (and non-glibc deps) from the SoftHSM snap into a tarball."""
    remote_archive = "/tmp/openbao-hsm-lib.tar.gz"
    script = f"""
set -euo pipefail
# Resolve the revision dir: plain find does not descend into /snap/*/current symlinks.
SNAP_ROOT="$(readlink -f /snap/{SOFTHSM_SNAP_NAME}/current)"
MODULE="$(find "$SNAP_ROOT" -name '{SOFTHSM_MODULE_NAME}' 2>/dev/null | head -n1)"
test -n "$MODULE"
STAGE=/tmp/openbao-hsm-libs
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -aL "$MODULE" "$STAGE/{SOFTHSM_MODULE_NAME}"
# Bundle shared-library dependencies SoftHSM needs inside the OpenBao snap.
# Ignore ldd failures (pipefail) when only system libs are linked.
set +e
DEPS="$(ldd "$MODULE" | awk '/=> \\// {{print $3}}')"
set -e
while read -r lib; do
  [ -z "$lib" ] && continue
  case "$lib" in
    */ld-linux*|*/libc.so*|*/libm.so*|*/libpthread.so*|*/libdl.so*|*/librt.so*|*/libgcc_s.so*)
      continue
      ;;
  esac
  cp -aL "$lib" "$STAGE/" || true
done <<< "$DEPS"
tar czf '{remote_archive}' -C "$STAGE" .
ls -la '{remote_archive}'
"""
    _unit_exec(juju, unit_name, "bash", "-lc", script)
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    juju.cli("scp", f"{unit_name}:{remote_archive}", str(dest))
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError(f"Failed to fetch SoftHSM hsm-lib archive to {dest}")
    logger.info("Fetched SoftHSM hsm-lib archive %s (%s bytes)", dest, dest.stat().st_size)
    return dest


def deploy_if_not_exists(  # noqa: C901
    juju: jubilant.Juju,
    app_name: str,
    charm_path: Path | None = None,
    num_units: int = 1,
    config: dict | None = None,
    channel: str | None = None,
    revision: int | None = None,
    series: str | None = None,
    trust: bool = False,
    constraints: dict | None = None,
    resources: dict | None = None,
) -> None:
    status = juju.status()
    if app_name in status.apps:
        return
    kwargs: dict[str, Any] = {}
    if config:
        kwargs["config"] = config
    if app_name == APP_NAME or (charm_path and Path(charm_path).name.startswith("openbao_")):
        kwargs["resources"] = openbao_charm_resources(resources)
    elif resources:
        kwargs["resources"] = resources
    if channel:
        kwargs["channel"] = channel
    if revision:
        kwargs["revision"] = revision
    if series:
        kwargs["base"] = series
    if trust:
        kwargs["trust"] = trust
    if constraints:
        kwargs["constraints"] = constraints
    try:
        juju.deploy(
            charm_path if charm_path else app_name,
            app_name,
            num_units=num_units,
            **kwargs,
        )
    except jubilant.CLIError as e:
        if "already exists" in (e.stderr or ""):
            logger.warning("Application `%s` already exists, skipping deploy", app_name)
            return
        raise


def get_juju_secret(juju: jubilant.Juju, label: str, fields: List[str]) -> List[str]:
    secrets = juju.secrets()
    try:
        secret = next(s for s in secrets if s.label == label)
        revealed = juju.show_secret(secret.uri, reveal=True)
    except StopIteration:
        # Fallback: look up directly by label in case juju.secrets() listing is incomplete
        revealed = juju.show_secret(label, reveal=True)
    return [revealed.content[field] for field in fields]


def get_openbao_pki_intermediate_ca_common_name(
    root_token: str, unit_address: str, mount: str
) -> str:
    openbao = OpenBao(
        url=f"https://{unit_address}:8200",
        token=root_token,
    )
    ca_cert: str = openbao.client.secrets.pki.read_ca_certificate(mount_point=mount)
    assert ca_cert, "No CA certificate found"
    loaded_certificate = x509.load_pem_x509_certificate(ca_cert.encode("utf-8"))
    return str(
        loaded_certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
    )


def run_action_on_leader(
    juju: jubilant.Juju,
    app_name: str,
    action_name: str,
    raise_on_error: bool = True,
    **kwargs: Any,
) -> dict:
    """Run an action on the leader unit of the given application.

    Wait for the action to complete and return the output.

    Args:
        juju: The Jubilant Juju instance.
        app_name: The name of the application to run the action on.
        action_name: The name of the action to run.
        raise_on_error: Whether to raise an error if the action fails.
        **kwargs: Additional keyword arguments. Underscores replaced with dashes.

    Returns:
        dict: The output of the action.
    """
    kwargs = {k.replace("_", "-"): v for k, v in kwargs.items()}
    task = juju.run(f"{app_name}/leader", action_name, kwargs, wait=120)
    logger.info(
        "Action `%s` on `%s/leader` completed with status `%s`. Results: %s",
        action_name,
        app_name,
        task.status,
        task.results,
    )
    if raise_on_error and task.status != "completed":
        raise ActionFailedError(f"Action {action_name} failed with status `{task.status}`.")
    return task.results


def refresh_application(juju: jubilant.Juju, app_name: str, charm_path: Path) -> None:
    resources = openbao_charm_resources()
    if app_name == APP_NAME and config.OPENBAO_SNAP_PATH:
        resources["openbao-snap"] = config.OPENBAO_SNAP_PATH
    juju.refresh(app_name, path=charm_path, resources=resources)


def configure_s3_and_create_backup(
    juju: jubilant.Juju,
    root_token: str,
    s3_endpoint: str,
    s3_access_key: str,
    s3_secret_key: str,
    s3_bucket: str,
    s3_region: str,
    kv_secret_value: str,
) -> None:
    """Configure the S3 integrator, write a KV secret, and create a backup."""
    run_action_on_leader(
        juju,
        S3_INTEGRATOR_APPLICATION_NAME,
        "sync-s3-credentials",
        access_key=s3_access_key,
        secret_key=s3_secret_key,
    )

    s3_config = {
        "endpoint": s3_endpoint,
        "bucket": s3_bucket,
        "region": s3_region,
    }
    juju.config(S3_INTEGRATOR_APPLICATION_NAME, s3_config)
    juju.wait(
        lambda s: jubilant.all_active(s, S3_INTEGRATOR_APPLICATION_NAME),
        timeout=SHORT_TIMEOUT,
    )

    if not has_relation(juju, APP_NAME, "s3-parameters"):
        juju.integrate(APP_NAME, S3_INTEGRATOR_APPLICATION_NAME)
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APP_NAME)
                and len(s.apps[APP_NAME].units) == NUM_OPENBAO_UNITS
                and all(u.juju_status.current == "idle" for u in s.apps[APP_NAME].units.values())
            ),
            timeout=SHORT_TIMEOUT,
        )

    leader_name = get_leader_unit_name(juju, APP_NAME)
    openbao = get_openbao_client(juju, leader_name, root_token)
    openbao.enable_kv_engine(path="kv/", description="Test KV Engine")
    openbao.write("kv/secret", {"key": kv_secret_value})

    run_action_on_leader(juju, APP_NAME, "create-backup", skip_verify=True)


def list_backups(juju: jubilant.Juju) -> list[str]:
    """List backups and return the backup IDs."""
    results = run_action_on_leader(juju, APP_NAME, "list-backups", skip_verify=True)
    assert results["backup-ids"] is not None
    backup_ids = json.loads(results["backup-ids"])
    assert len(backup_ids) > 0
    return backup_ids


def restore_backup(
    juju: jubilant.Juju,
    root_token: str,
    kv_secret_value: str,
) -> None:
    """Restore the most recent backup and verify the KV secret is restored."""
    backup_ids = list_backups(juju)
    backup_id = backup_ids[-1]

    leader_name = get_leader_unit_name(juju, APP_NAME)
    openbao = get_openbao_client(juju, leader_name, root_token)

    assert openbao.read("kv/secret") == {"key": kv_secret_value}
    openbao.delete("kv/secret")
    assert openbao.read("kv/secret") is None

    backup_action_output = run_action_on_leader(
        juju, APP_NAME, "restore-backup", skip_verify=True, backup_id=backup_id
    )

    assert openbao.read("kv/secret") == {"key": kv_secret_value}
    assert backup_action_output["restored"] == backup_id
