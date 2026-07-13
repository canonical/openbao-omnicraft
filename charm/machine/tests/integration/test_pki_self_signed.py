#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for PKI self-signed CA functionality.

This module tests the PKI feature when OpenBao uses its own self-signed CA
instead of relying on an external CA provider via the tls-certificates-pki relation.
"""

import logging
from collections import namedtuple
from pathlib import Path

import jubilant
import pytest

from config import (
    APP_NAME,
    JUJU_FAST_INTERVAL,
    MATCHING_COMMON_NAME,
    NUM_OPENBAO_UNITS,
    OPENBAO_PKI_REQUIRER_APPLICATION_NAME,
    OPENBAO_PKI_REQUIRER_CHANNEL,
    OPENBAO_PKI_REQUIRER_REVISION,
    SHORT_TIMEOUT,
)
from helpers import (
    deploy_openbao,
    fast_forward,
    get_leader_unit_address,
    get_openbao_pki_intermediate_ca_common_name,
    get_openbao_token_and_unseal_key,
    initialize_unseal_authorize_openbao,
    run_get_certificate_action,
    wait_for_certificate_to_be_provided,
)

logger = logging.getLogger(__name__)

OpenBaoInit = namedtuple("OpenBaoInit", ["root_token", "unseal_key"])


@pytest.fixture(scope="module")
def deploy(juju: jubilant.Juju, openbao_charm_path: Path, skip_deploy: bool) -> OpenBaoInit:
    """Build and deploy the application."""
    if skip_deploy:
        logger.info("Skipping deployment due to --no-deploy flag")
        root_token, key = get_openbao_token_and_unseal_key(juju, APP_NAME)
        return OpenBaoInit(root_token, key)

    deploy_openbao(juju, charm_path=openbao_charm_path, num_openbaos=NUM_OPENBAO_UNITS)
    juju.deploy(
        OPENBAO_PKI_REQUIRER_APPLICATION_NAME,
        channel=OPENBAO_PKI_REQUIRER_CHANNEL,
        revision=OPENBAO_PKI_REQUIRER_REVISION,
        config={
            "common_name": f"test.{MATCHING_COMMON_NAME}",
            "sans_dns": f"test.{MATCHING_COMMON_NAME}",
        },
    )

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, OPENBAO_PKI_REQUIRER_APPLICATION_NAME)
                and jubilant.all_blocked(s, APP_NAME)
                and len(s.apps[APP_NAME].units) == NUM_OPENBAO_UNITS
            ),
            timeout=1000,
        )
    root_token, unseal_key = initialize_unseal_authorize_openbao(juju, APP_NAME)
    return OpenBaoInit(root_token, unseal_key)


@pytest.mark.abort_on_fail
def test_given_no_external_ca_and_common_name_configured_when_integrate_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    """Test that OpenBao becomes active when PKI is configured without external CA."""
    common_name = "self-signed-ca.example.com"
    juju.config(APP_NAME, {"pki_ca_common_name": common_name})
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, APP_NAME),
            timeout=SHORT_TIMEOUT,
        )


@pytest.mark.abort_on_fail
def test_given_self_signed_ca_configured_when_integrate_openbao_pki_then_certificate_is_issued(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    """Test that certificates are issued using the self-signed CA."""
    common_name = MATCHING_COMMON_NAME
    juju.config(
        APP_NAME,
        {"pki_ca_common_name": common_name, "pki_allow_subdomains": "true"},
    )
    juju.integrate(
        f"{APP_NAME}:openbao-pki",
        f"{OPENBAO_PKI_REQUIRER_APPLICATION_NAME}:certificates",
    )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APP_NAME, OPENBAO_PKI_REQUIRER_APPLICATION_NAME)
                and len(s.apps[APP_NAME].units) == NUM_OPENBAO_UNITS
                and len(s.apps[OPENBAO_PKI_REQUIRER_APPLICATION_NAME].units) == 1
            ),
            timeout=SHORT_TIMEOUT,
        )
        wait_for_certificate_to_be_provided(juju)

    leader_unit_address = get_leader_unit_address(juju)
    current_issuers_common_name = get_openbao_pki_intermediate_ca_common_name(
        root_token=deploy.root_token,
        unit_address=leader_unit_address,
        mount="charm-pki",
    )
    assert current_issuers_common_name == common_name

    action_output = run_get_certificate_action(juju)
    assert action_output["certificate"] is not None
    assert action_output["ca-certificate"] is not None
    assert action_output["csr"] is not None


@pytest.mark.abort_on_fail
def test_given_self_signed_ca_when_common_name_changed_then_new_ca_is_generated(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    """Test that changing pki_ca_common_name regenerates the self-signed CA."""
    new_common_name = "rotated-ca.example.com"

    leader_unit_address = get_leader_unit_address(juju)
    old_common_name = get_openbao_pki_intermediate_ca_common_name(
        root_token=deploy.root_token,
        unit_address=leader_unit_address,
        mount="charm-pki",
    )

    juju.config(
        APP_NAME,
        {"pki_ca_common_name": new_common_name, "pki_allow_subdomains": "true"},
    )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APP_NAME)
                and len(s.apps[APP_NAME].units) == NUM_OPENBAO_UNITS
            ),
            timeout=SHORT_TIMEOUT,
        )
        wait_for_certificate_to_be_provided(juju)

    current_common_name = get_openbao_pki_intermediate_ca_common_name(
        root_token=deploy.root_token,
        unit_address=leader_unit_address,
        mount="charm-pki",
    )
    assert current_common_name == new_common_name
    assert current_common_name != old_common_name
