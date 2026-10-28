# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
import time
from collections import namedtuple
from pathlib import Path

import jubilant
import pytest
import requests

from config import (
    APPLICATION_NAME,
    JUJU_FAST_INTERVAL,
    NUM_OPENBAO_UNITS,
    SELF_SIGNED_CERTIFICATES_APPLICATION_NAME,
    SELF_SIGNED_CERTIFICATES_CHANNEL,
    SELF_SIGNED_CERTIFICATES_REVISION,
    UNMATCHING_COMMON_NAME,
)
from helpers import (
    deploy_openbao,
    fast_forward,
    get_leader_unit_name,
    get_openbao_token_and_unseal_key,
    get_unit_address,
    initialize_unseal_authorize_openbao,
)

logger = logging.getLogger(__name__)

OpenBaoInit = namedtuple("OpenBaoInit", ["root_token", "unseal_key"])

ACME_MOUNT = "charm-acme"


@pytest.fixture(scope="module")
def deploy(juju: jubilant.Juju, openbao_charm_path: Path, skip_deploy: bool) -> OpenBaoInit:
    """Build and deploy the application."""
    if skip_deploy:
        logger.info("Skipping deployment due to --no-deploy flag")
        root_token, key = get_openbao_token_and_unseal_key(juju, APPLICATION_NAME)
        return OpenBaoInit(root_token, key)
    deploy_openbao(juju, charm_path=openbao_charm_path, num_units=NUM_OPENBAO_UNITS)
    juju.deploy(
        SELF_SIGNED_CERTIFICATES_APPLICATION_NAME,
        channel=SELF_SIGNED_CERTIFICATES_CHANNEL,
        revision=SELF_SIGNED_CERTIFICATES_REVISION,
    )

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_blocked(s, APPLICATION_NAME)
                and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
                and jubilant.all_active(s, SELF_SIGNED_CERTIFICATES_APPLICATION_NAME)
            ),
        )

    root_token, unseal_key = initialize_unseal_authorize_openbao(juju, APPLICATION_NAME)
    return OpenBaoInit(root_token, unseal_key)


@pytest.mark.abort_on_fail
def test_given_tls_certificates_acme_relation_when_integrate_then_status_is_active_and_acme_configured(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    juju.config(
        APPLICATION_NAME,
        {
            "acme_ca_common_name": UNMATCHING_COMMON_NAME,
            "acme_allow_any_name": "true",
            "acme_allow_subdomains": "true",
        },
    )
    juju.integrate(
        f"{APPLICATION_NAME}:tls-certificates-acme",
        f"{SELF_SIGNED_CERTIFICATES_APPLICATION_NAME}:certificates",
    )
    fast_interval_seconds = int(JUJU_FAST_INTERVAL.rstrip("s"))
    # Keep fast_forward active while checking ACME - the reconcile loop is needed
    # to complete ACME setup: https://warthogs.atlassian.net/browse/TLSENG-766
    response = None
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(
                s, APPLICATION_NAME, SELF_SIGNED_CERTIFICATES_APPLICATION_NAME
            ),
        )
        leader_name = get_leader_unit_name(juju, APPLICATION_NAME)
        leader_address = get_unit_address(juju, leader_name)
        acme_url = f"https://{leader_address}:8200/v1/{ACME_MOUNT}/acme/directory"
        for _ in range(3):
            response = requests.get(acme_url, verify=False)
            if response.status_code == 200 and "newNonce" in response.json():
                break
            logger.warning("ACME not available yet (status=%s), retrying...", response.status_code)
            time.sleep(fast_interval_seconds)
    assert response is not None and response.status_code == 200, (
        f"ACME endpoint returned {response.status_code if response else 'no response'}"
    )
