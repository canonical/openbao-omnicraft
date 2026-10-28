import logging
from collections import namedtuple
from pathlib import Path

import jubilant
import pytest

from config import (
    APP_NAME,
    GRAFANA_AGENT_APPLICATION_NAME,
    GRAFANA_AGENT_CHANNEL,
    GRAFANA_AGENT_REVISION,
    JUJU_FAST_INTERVAL,
    NUM_OPENBAO_UNITS,
    SHORT_TIMEOUT,
)
from helpers import (
    deploy_openbao,
    fast_forward,
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
        root_token, key = get_openbao_token_and_unseal_key(juju, APP_NAME)
        return OpenBaoInit(root_token, key)
    deploy_openbao(
        juju,
        charm_path=openbao_charm_path,
        num_openbaos=NUM_OPENBAO_UNITS,
    )
    juju.deploy(
        GRAFANA_AGENT_APPLICATION_NAME,
        GRAFANA_AGENT_APPLICATION_NAME,
        base="ubuntu@24.04",
        channel=GRAFANA_AGENT_CHANNEL,
        revision=GRAFANA_AGENT_REVISION,
    )

    # When waiting for OpenBao to go to the blocked state, we may need an update
    # status event to recognize that the API is available, so we wait in
    # fast-forward.
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_blocked(s, APP_NAME)
                and len(s.apps[APP_NAME].units) == NUM_OPENBAO_UNITS
            ),
            timeout=1000,
        )
    root_token, unseal_key = initialize_unseal_authorize_openbao(juju, APP_NAME)
    return OpenBaoInit(root_token, unseal_key)


@pytest.mark.abort_on_fail
def test_given_grafana_agent_deployed_when_relate_to_grafana_agent_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    juju.integrate(
        f"{APP_NAME}:cos-agent",
        f"{GRAFANA_AGENT_APPLICATION_NAME}:cos-agent",
    )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, APP_NAME),
            timeout=SHORT_TIMEOUT,
        )
