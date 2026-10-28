# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""PKCS#11 HSM auto-unseal integration tests (SoftHSM).

This suite:

1. Deploys the OpenBao machine charm (placeholder ``hsm-lib``).
2. On the **test runner**, creates a SoftHSM token + AES key and packs
   libs + tokens + ``softhsm2.conf`` + ``openbao.env`` as ``hsm-lib``.
3. Attaches the resource and configures the HSM Juju secret.
4. Waits for the charm to unpack, rewrite the PKCS#11 seal, and restart.
5. Initializes with PKCS#11, verifies seal type, restart auto-unseal, and charm
   authorization.

Requires SoftHSM via ``snap install softhsm`` and OpenSC on the
runner, plus an amd64/arm64 OpenBao snap that ships ``plugins/openbao-plugin-kms-pkcs11``.
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
    HSM_LIB_DIR,
    SOFTHSM_CONF_IN_SNAP,
    SOFTHSM_KEY_LABEL,
    SOFTHSM_MODULE_NAME,
    SOFTHSM_TOKEN_LABEL,
    authorize_charm_and_wait,
    build_host_softhsm_hsm_lib_tarball,
    deploy_openbao,
    fast_forward,
    get_leader_unit_name,
    get_openbao_client,
    initialize_openbao_leader,
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


def _assert_hsm_lib_unpacked(juju: jubilant.Juju, unit_name: str) -> None:
    """Assert the charm unpacked the SoftHSM archive and installed openbao.env."""
    juju.exec(
        "bash",
        "-lc",
        f"""
set -euo pipefail
test -f '{HSM_LIB_DIR}/{SOFTHSM_MODULE_NAME}'
test -f '{SOFTHSM_CONF_IN_SNAP}'
test -f '{HSM_LIB_DIR}/openbao.env'
grep -q 'directories.tokendir' '{SOFTHSM_CONF_IN_SNAP}'
grep -q 'SOFTHSM2_CONF' '{HSM_LIB_DIR}/openbao.env'
grep -q 'SOFTHSM2_CONF' /var/snap/openbao/common/openbao.env
test -d '{HSM_LIB_DIR}/tokens'
find '{HSM_LIB_DIR}/tokens' -name token.object | grep -q .
""",
        unit=unit_name,
    )
    logger.info(
        "hsm-lib unpacked on %s (token=%s key=%s)",
        unit_name,
        SOFTHSM_TOKEN_LABEL,
        SOFTHSM_KEY_LABEL,
    )


@pytest.mark.abort_on_fail
def test_given_softhsm_configured_when_initialized_then_auto_unseals(
    juju: jubilant.Juju,
    openbao_charm_path: Path,
):
    """Deploy OpenBao, attach host-built SoftHSM hsm-lib, initialize PKCS#11, auto-unseal."""
    deploy_openbao(juju, num_openbaos=1, charm_path=openbao_charm_path)

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

    # SoftHSM lives on the runner; the charm only unpacks the attached tarball.
    # Juju snap cannot read host /tmp for attach-resource; keep the archive under $HOME.
    with tempfile.TemporaryDirectory(prefix="openbao-hsm-", dir=Path.home()) as tmp:
        hsm_resource, secret_content = build_host_softhsm_hsm_lib_tarball(
            Path(tmp) / "hsm-lib.tar.gz"
        )
        assert secret_content["token-label"] == SOFTHSM_TOKEN_LABEL
        assert secret_content["key-label"] == SOFTHSM_KEY_LABEL
        assert secret_content["lib"] == SOFTHSM_MODULE_NAME

        juju.cli("attach-resource", APP_NAME, f"hsm-lib={hsm_resource}")

        secret_name = "hsm-config"
        secret_id = str(juju.add_secret(secret_name, secret_content))
        juju.grant_secret(secret_name, APP_NAME)
        juju.config(APP_NAME, {"hsm-config-secret-id": secret_id})

        # Charm unpacks hsm-lib, rewrites the PKCS#11 seal, and restarts OpenBao.
        with fast_forward(juju, JUJU_FAST_INTERVAL):
            _wait_for_pkcs11_seal_config(juju, leader_name, timeout=600)
            _assert_hsm_lib_unpacked(juju, leader_name)
            wait_for_status_message(
                juju,
                expected_message="Please initialize OpenBao",
                app_name=APP_NAME,
                timeout=600,
            )

    root_token, recovery_key = initialize_openbao_leader(juju, APP_NAME)
    assert recovery_key, "PKCS#11 initialization should return a recovery key"
    openbao = get_openbao_client(juju, leader_name, root_token)
    openbao.wait_for_node_to_be_unsealed()
    assert openbao.client.seal_status["type"] == "pkcs11"  # type: ignore[reportIndexIssue]
    assert not openbao.is_sealed()

    # Auto-unseal across restart (charm already bounced once when seal became ready).
    juju.ssh(leader_name, "sudo snap restart openbao")
    openbao.wait_for_node_to_be_unsealed()
    assert not openbao.is_sealed()
    assert openbao.client.seal_status["type"] == "pkcs11"  # type: ignore[reportIndexIssue]

    authorize_charm_and_wait(juju, root_token)

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda status: APP_NAME in status.apps and jubilant.all_active(status, APP_NAME),
            timeout=600,
        )
