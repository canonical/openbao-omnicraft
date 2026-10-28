# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
from collections import namedtuple
from pathlib import Path

import jubilant
import pytest

from config import (
    APPLICATION_NAME,
    LOKI_APPLICATION_NAME,
    LOKI_CHANNEL,
    LOKI_REVISION,
    NUM_OPENBAO_UNITS,
    PROMETHEUS_APPLICATION_NAME,
    PROMETHEUS_CHANNEL,
    PROMETHEUS_REVISION,
    SHORT_TIMEOUT,
)
from helpers import (
    deploy_openbao,
    get_openbao_token_and_unseal_key,
    initialize_unseal_authorize_openbao,
)

logger = logging.getLogger(__name__)

OpenBaoInit = namedtuple("OpenBaoInit", ["root_token", "unseal_key"])


@pytest.fixture(scope="module")
def deploy(juju: jubilant.Juju, openbao_charm_path: Path, skip_deploy: bool) -> OpenBaoInit:
    """Build and deploy the application."""
    if skip_deploy:
        logger.info("Skipping deployment due to --no-deploy flag")
        root_token, key = get_openbao_token_and_unseal_key(juju, APPLICATION_NAME)
        return OpenBaoInit(root_token, key)
    deploy_openbao(
        juju,
        charm_path=openbao_charm_path,
        num_units=NUM_OPENBAO_UNITS,
    )
    juju.wait(
        lambda s: (
            APPLICATION_NAME in s.apps
            and jubilant.all_blocked(s, APPLICATION_NAME)
            and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
        ),
        error=None,
    )
    root_token, unseal_key = initialize_unseal_authorize_openbao(juju, APPLICATION_NAME)
    juju.deploy(
        PROMETHEUS_APPLICATION_NAME,
        trust=True,
        channel=PROMETHEUS_CHANNEL,
        revision=PROMETHEUS_REVISION,
    )
    juju.deploy(
        LOKI_APPLICATION_NAME,
        trust=True,
        channel=LOKI_CHANNEL,
        revision=LOKI_REVISION,
    )
    juju.wait(
        lambda s: (
            APPLICATION_NAME in s.apps
            and PROMETHEUS_APPLICATION_NAME in s.apps
            and LOKI_APPLICATION_NAME in s.apps
            and jubilant.all_active(s, APPLICATION_NAME)
            and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
        ),
        error=None,
    )
    return OpenBaoInit(root_token, unseal_key)


@pytest.mark.abort_on_fail
def test_given_prometheus_deployed_when_relate_openbao_to_prometheus_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    juju.integrate(
        f"{APPLICATION_NAME}:metrics-endpoint",
        f"{PROMETHEUS_APPLICATION_NAME}:metrics-endpoint",
    )
    juju.wait(
        lambda s: jubilant.all_active(s, APPLICATION_NAME, PROMETHEUS_APPLICATION_NAME),
        timeout=SHORT_TIMEOUT,
    )


@pytest.mark.abort_on_fail
def test_given_loki_deployed_when_relate_openbao_to_loki_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    juju.integrate(
        f"{APPLICATION_NAME}:logging",
        LOKI_APPLICATION_NAME,
    )
    juju.wait(
        lambda s: jubilant.all_active(s, APPLICATION_NAME, LOKI_APPLICATION_NAME),
        timeout=SHORT_TIMEOUT,
    )
