#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Minimal integration test: deploy OpenBao and unseal it."""

import logging
from pathlib import Path

import jubilant
import pytest

from config import APP_NAME
from helpers import (
    authorize_charm_and_wait,
    deploy_openbao,
    fast_forward,
    get_leader_unit_name,
    get_openbao_client,
    initialize_openbao_leader,
    unseal_all_openbao_units,
    wait_for_status_message,
)

logger = logging.getLogger(__name__)

JUJU_FAST_INTERVAL = "20s"


@pytest.mark.abort_on_fail
def test_deploy_and_unseal(juju: jubilant.Juju, openbao_charm_path: Path):
    """Deploy OpenBao, initialize, and unseal using self-signed TLS mode."""
    deploy_openbao(juju, num_openbaos=1, charm_path=openbao_charm_path)

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        wait_for_status_message(
            juju,
            expected_message="Please initialize OpenBao or integrate with an auto-unseal provider",
            app_name=APP_NAME,
            timeout=600,
        )

    root_token, unseal_key = initialize_openbao_leader(juju, APP_NAME)

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        unseal_all_openbao_units(juju, unseal_key)

    leader_name = get_leader_unit_name(juju, APP_NAME)
    openbao = get_openbao_client(juju, leader_name, root_token)
    assert not openbao.is_sealed(), "OpenBao should be unsealed"

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        authorize_charm_and_wait(juju, root_token)

    logger.info("OpenBao deployed, unsealed, and active on s390x")
