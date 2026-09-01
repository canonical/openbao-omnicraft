# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""PKCS#11 HSM auto-unseal integration tests (SoftHSM).

This suite installs the ``softhsm`` snap on the OpenBao unit (idempotent; local
``.snap`` via ``--softhsm-snap-path`` when not in the store), creates a token and AES
key under snap-common, attaches ``libsofthsm2.so`` (plus deps) as the ``hsm-lib``
resource, and verifies PKCS#11 auto-unseal across a restart.

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
    SOFTHSM_MODULE_NAME,
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


@pytest.mark.abort_on_fail
def test_given_softhsm_configured_when_initialized_then_auto_unseals(
    juju: jubilant.Juju,
    openbao_charm_path: Path,
):
    """Deploy OpenBao, auto-provision SoftHSM, initialize with PKCS#11, restart."""
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
    secret_content = setup_softhsm_on_unit(juju, leader_name)
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

    root_token, recovery_key = initialize_openbao_leader(juju, APP_NAME)
    assert recovery_key, "PKCS#11 initialization should return a recovery key"
    openbao = get_openbao_client(juju, leader_name, root_token)
    openbao.wait_for_node_to_be_unsealed()
    assert openbao.client.seal_status["type"] == "pkcs11"  # type: ignore[reportIndexIssue]
    assert not openbao.is_sealed()

    juju.ssh(leader_name, "sudo snap restart openbao")
    openbao.wait_for_node_to_be_unsealed()
    assert not openbao.is_sealed()
    assert openbao.client.seal_status["type"] == "pkcs11"  # type: ignore[reportIndexIssue]

    authorize_charm_and_wait(juju, root_token)
