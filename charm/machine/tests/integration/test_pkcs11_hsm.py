# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""PKCS#11 HSM auto-unseal integration tests (SoftHSM).

This suite:

1. Deploys the OpenBao machine charm (placeholder ``hsm-lib``).
2. Installs the ``softhsm`` snap from the Snap Store on the unit.
3. Creates a SoftHSM token + AES key, copies the token store into OpenBao
   snap-common, and exports ``SOFTHSM2_CONF`` in ``openbao.env``.
4. Attaches ``libsofthsm2.so`` (+ deps) as the ``hsm-lib`` resource and configures
   the HSM Juju secret.
5. Initializes with PKCS#11, verifies seal type, restart auto-unseal, and charm
   authorization.

Requires an amd64/arm64 OpenBao snap that ships ``plugins/openbao-plugin-kms-pkcs11``.
YubiHSM / YubiKey coverage is deferred.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

import jubilant
import pytest

from config import APP_NAME, JUJU_FAST_INTERVAL
from helpers import (
    SOFTHSM_CONF_PATH,
    SOFTHSM_KEY_LABEL,
    SOFTHSM_MODULE_NAME,
    SOFTHSM_TOKEN_LABEL,
    authorize_charm_and_wait,
    build_softhsm_hsm_lib_tarball,
    deploy_openbao,
    fast_forward,
    get_leader_unit_name,
    get_openbao_client,
    initialize_openbao_leader,
    setup_softhsm_on_unit,
    wait_for_status_message,
)

logger = logging.getLogger(__name__)


def _wait_for_pkcs11_seal_config(juju: jubilant.Juju, unit_name: str, timeout: int = 600) -> None:
    """Wait until the unit's OpenBao config contains a PKCS#11 seal stanza."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = juju.exec(
            "grep -F 'seal \"pkcs11\"' /var/snap/openbao/common/openbao-config.hcl "
            f"&& grep -F '{SOFTHSM_MODULE_NAME}' /var/snap/openbao/common/openbao-config.hcl "
            "|| true",
            unit=unit_name,
        )
        stdout = result.stdout or ""
        if 'seal "pkcs11"' in stdout and SOFTHSM_MODULE_NAME in stdout:
            return
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for PKCS#11 seal config on {unit_name}")


def _assert_softhsm_runtime_ready(juju: jubilant.Juju, unit_name: str) -> None:
    """Assert SoftHSM conf/env and token files are staged for the OpenBao snap."""
    juju.exec(
        "bash",
        "-lc",
        f"""
set -euo pipefail
test -f '{SOFTHSM_CONF_PATH}'
grep -q 'directories.tokendir' '{SOFTHSM_CONF_PATH}'
grep -q '^export SOFTHSM2_CONF={SOFTHSM_CONF_PATH}$' /var/snap/openbao/common/openbao.env
test -d /var/snap/openbao/common/softhsm/tokens
find /var/snap/openbao/common/softhsm/tokens -name token.object | grep -q .
""",
        unit=unit_name,
    )
    logger.info(
        "SoftHSM runtime ready on %s (token=%s key=%s)",
        unit_name,
        SOFTHSM_TOKEN_LABEL,
        SOFTHSM_KEY_LABEL,
    )


@pytest.mark.abort_on_fail
def test_given_softhsm_configured_when_initialized_then_auto_unseals(
    juju: jubilant.Juju,
    openbao_charm_path: Path,
):
    """Deploy OpenBao, provision SoftHSM from the store, initialize PKCS#11, restart."""
    deploy_openbao(juju, num_openbaos=1, charm_path=openbao_charm_path)

    # Wait until the snap/charm are far enough along that snap-common exists.
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        wait_for_status_message(
            juju,
            expected_message=(
                "Please initialize OpenBao or integrate with an auto-unseal provider"
            ),
            app_name=APP_NAME,
            timeout=600,
        )

    leader_name = get_leader_unit_name(juju, APP_NAME)

    # Install SoftHSM from the Snap Store, create token+key, copy into OpenBao common.
    secret_content = setup_softhsm_on_unit(juju, leader_name)
    _assert_softhsm_runtime_ready(juju, leader_name)
    assert secret_content["token-label"] == SOFTHSM_TOKEN_LABEL
    assert secret_content["key-label"] == SOFTHSM_KEY_LABEL
    assert secret_content["lib"] == SOFTHSM_MODULE_NAME

    # Juju is snap-confined and cannot scp into host /tmp; keep the archive under $HOME.
    with tempfile.TemporaryDirectory(prefix="openbao-hsm-", dir=Path.home()) as tmp:
        hsm_resource = build_softhsm_hsm_lib_tarball(
            juju, leader_name, Path(tmp) / "hsm-lib.tar.gz"
        )
        juju.cli("attach-resource", APP_NAME, f"hsm-lib={hsm_resource}")

        secret_name = "hsm-config"
        secret_id = str(juju.add_secret(secret_name, secret_content))
        juju.grant_secret(secret_name, APP_NAME)
        juju.config(APP_NAME, {"hsm-config-secret-id": secret_id})

        # Keep the archive until attach-resource has uploaded it; then wait for
        # the charm to render the PKCS#11 seal. With HSM configured the status
        # shortens to "Please initialize OpenBao" (no auto-unseal-provider hint).
        with fast_forward(juju, JUJU_FAST_INTERVAL):
            _wait_for_pkcs11_seal_config(juju, leader_name, timeout=600)
            wait_for_status_message(
                juju,
                expected_message="Please initialize OpenBao",
                app_name=APP_NAME,
                timeout=600,
            )

    # SoftHSM env is already set; restart so the seal plugin sees SOFTHSM2_CONF
    # before initialization if config-changed did not bounce the snap.
    juju.ssh(leader_name, "sudo snap restart openbao")

    root_token, recovery_key = initialize_openbao_leader(juju, APP_NAME)
    assert recovery_key, "PKCS#11 initialization should return a recovery key"
    openbao = get_openbao_client(juju, leader_name, root_token)
    openbao.wait_for_node_to_be_unsealed()
    assert openbao.client.seal_status["type"] == "pkcs11"  # type: ignore[reportIndexIssue]
    assert not openbao.is_sealed()

    # Auto-unseal across restart.
    juju.ssh(leader_name, "sudo snap restart openbao")
    openbao.wait_for_node_to_be_unsealed()
    assert not openbao.is_sealed()
    assert openbao.client.seal_status["type"] == "pkcs11"  # type: ignore[reportIndexIssue]

    authorize_charm_and_wait(juju, root_token)

    # Charm should settle active after authorization.
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda status: APP_NAME in status.apps and jubilant.all_active(status, APP_NAME),
            timeout=600,
        )
