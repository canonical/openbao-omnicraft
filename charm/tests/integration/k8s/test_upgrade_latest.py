# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
from pathlib import Path

import jubilant
import pytest

from config import (
    APPLICATION_NAME,
    DEPLOY_TIMEOUT,
    JUJU_FAST_INTERVAL,
    NUM_OPENBAO_UNITS,
)
from helpers import (
    deploy_openbao,
    fast_forward,
    get_ca_cert_file_location,
    initialize_unseal_authorize_openbao,
    refresh_application,
    unseal_all_openbao_units,
)

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.skip(
    reason="Upgrade tests refresh from the Charmhub `vault` charm, which is not applicable to the renamed `openbao` charm until a first openbao revision is published."
)

CURRENT_TRACK_LATEST_STABLE_CHANNEL = "1.18/stable"


@pytest.mark.abort_on_fail
def test_given_latest_stable_revision_in_track_when_refresh_then_status_is_active(
    juju: jubilant.Juju, openbao_charm_path: Path
):
    logger.info("Deploying openbao from Charmhub")
    deploy_openbao(
        juju,
        num_units=NUM_OPENBAO_UNITS,
        channel=CURRENT_TRACK_LATEST_STABLE_CHANNEL,
    )
    juju.wait(
        lambda s: (
            jubilant.all_blocked(s, APPLICATION_NAME)
            and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
        ),
        timeout=DEPLOY_TIMEOUT,
    )
    root_token, unseal_key = initialize_unseal_authorize_openbao(juju, APPLICATION_NAME)

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APPLICATION_NAME)
                and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
            ),
            timeout=DEPLOY_TIMEOUT,
        )
        logger.info("Refreshing openbao from built charm")
        refresh_application(juju, APPLICATION_NAME, openbao_charm_path)

    logger.info("Waiting for openbao to be blocked after refresh")
    juju.wait(
        lambda s: (
            jubilant.all_blocked(s, APPLICATION_NAME)
            and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
        ),
        timeout=DEPLOY_TIMEOUT,
    )

    ca_file = get_ca_cert_file_location(juju, APPLICATION_NAME)
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        unseal_all_openbao_units(juju, unseal_key, root_token, ca_file)

        logger.info("Waiting for openbao to be active after refresh")
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APPLICATION_NAME)
                and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
            ),
            timeout=DEPLOY_TIMEOUT,
        )
