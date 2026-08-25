# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""PKCS#11 HSM auto-unseal integration tests.

Manual suites (skipped unless the matching env flag is set):

- SoftHSM smoke (preferred for CI-ish validation): set ``OPENBAO_HSM_SOFTHSM=1``
  and the same ``OPENBAO_HSM_*`` variables as below, pointing ``OPENBAO_HSM_LIB``
  at SoftHSM's ``libsofthsm2.so``. No USB or ``pcscd`` required.
- YubiKey: set ``OPENBAO_HSM_YUBIKEY=1``, attach a YubiKey (or USB passthrough),
  install ``pcscd`` and the vendor PKCS#11 library.

Required environment (either flag):

- ``OPENBAO_HSM_LIB`` — path to the PKCS#11 library
- ``OPENBAO_HSM_PIN`` — token PIN
- ``OPENBAO_HSM_SLOT`` and/or ``OPENBAO_HSM_TOKEN_LABEL``
- ``OPENBAO_HSM_KEY_LABEL`` and/or ``OPENBAO_HSM_KEY_ID``

The AES/RSA key must already exist on the token. OpenBao will not create it.
The OpenBao snap must ship ``plugins/openbao-plugin-kms-pkcs11``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import jubilant
import pytest

from config import APP_NAME, JUJU_FAST_INTERVAL
from helpers import (
    authorize_charm_and_wait,
    deploy_openbao,
    fast_forward,
    get_leader_unit_name,
    get_openbao_client,
    initialize_openbao_leader,
    wait_for_status_message,
)

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(
    not (os.environ.get("OPENBAO_HSM_YUBIKEY") or os.environ.get("OPENBAO_HSM_SOFTHSM")),
    reason="Set OPENBAO_HSM_SOFTHSM=1 or OPENBAO_HSM_YUBIKEY=1 to run this suite.",
)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(
            f"{name} must be set when OPENBAO_HSM_SOFTHSM=1 or OPENBAO_HSM_YUBIKEY=1"
        )
    return value


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _hsm_secret_content() -> dict[str, str]:
    content = {"pin": _required_env("OPENBAO_HSM_PIN")}
    if slot := _optional_env("OPENBAO_HSM_SLOT"):
        content["slot"] = slot
    if token_label := _optional_env("OPENBAO_HSM_TOKEN_LABEL"):
        content["token-label"] = token_label
    if key_label := _optional_env("OPENBAO_HSM_KEY_LABEL"):
        content["key-label"] = key_label
    if key_id := _optional_env("OPENBAO_HSM_KEY_ID"):
        content["key-id"] = key_id
    if "slot" not in content and "token-label" not in content:
        pytest.fail("Set OPENBAO_HSM_SLOT and/or OPENBAO_HSM_TOKEN_LABEL")
    if "key-label" not in content and "key-id" not in content:
        pytest.fail("Set OPENBAO_HSM_KEY_LABEL and/or OPENBAO_HSM_KEY_ID")
    return content


@pytest.mark.abort_on_fail
def test_given_hsm_when_pkcs11_configured_then_openbao_auto_unseals(
    juju: jubilant.Juju, openbao_charm_path: Path
):
    """Deploy OpenBao with PKCS#11 seal, initialize, and confirm auto-unseal.

    SoftHSM: install SoftHSM, create token+key, point OPENBAO_HSM_LIB at libsofthsm2.so.
    YubiKey: pass the token into the machine, install pcscd and the vendor library,
    create the unseal key (pkcs11-tool / yubico-piv-tool).
    """
    hsm_lib = Path(_required_env("OPENBAO_HSM_LIB"))
    if not hsm_lib.is_file():
        pytest.fail(f"OPENBAO_HSM_LIB does not exist: {hsm_lib}")

    deploy_openbao(juju, num_openbaos=1, charm_path=openbao_charm_path)
    juju.cli("attach-resource", APP_NAME, f"hsm-lib={hsm_lib}")

    secret_name = "hsm-config"
    secret_id = str(juju.add_secret(secret_name, _hsm_secret_content()))
    juju.grant_secret(secret_name, APP_NAME)
    juju.config(APP_NAME, {"hsm-config-secret-id": secret_id})

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        wait_for_status_message(
            juju,
            expected_message="Please initialize OpenBao",
            app_name=APP_NAME,
            timeout=600,
        )

    root_token, recovery_key = initialize_openbao_leader(juju, APP_NAME)
    assert recovery_key, "PKCS#11 initialization should return a recovery key"
    leader_name = get_leader_unit_name(juju, APP_NAME)
    openbao = get_openbao_client(juju, leader_name, root_token)
    openbao.wait_for_node_to_be_unsealed()
    assert openbao.client.seal_status["type"] == "pkcs11"
    assert not openbao.is_sealed()

    juju.ssh(leader_name, "sudo snap restart openbao")
    openbao.wait_for_node_to_be_unsealed()
    assert not openbao.is_sealed()

    authorize_charm_and_wait(juju, root_token)
