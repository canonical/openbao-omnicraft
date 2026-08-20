import logging
from collections import namedtuple
from pathlib import Path

import jubilant
import pytest

from config import (
    APP_NAME,
    HAPROXY_APPLICATION_NAME,
    HAPROXY_REVISION,
    JUJU_FAST_INTERVAL,
    NUM_OPENBAO_UNITS,
    SELF_SIGNED_CERTIFICATES_APPLICATION_NAME,
    SELF_SIGNED_CERTIFICATES_REVISION,
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
        SELF_SIGNED_CERTIFICATES_APPLICATION_NAME,
        SELF_SIGNED_CERTIFICATES_APPLICATION_NAME,
        channel="1/stable",
        revision=SELF_SIGNED_CERTIFICATES_REVISION,
    )
    juju.deploy(
        HAPROXY_APPLICATION_NAME,
        HAPROXY_APPLICATION_NAME,
        channel="2.8/edge",
        revision=HAPROXY_REVISION,
    )

    # When waiting for OpenBao to go to the blocked state, we may need an update
    # status event to recognize that the API is available, so we wait in
    # fast-forward.
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, SELF_SIGNED_CERTIFICATES_APPLICATION_NAME)
                and jubilant.all_active(s, HAPROXY_APPLICATION_NAME)
                and jubilant.all_blocked(s, APP_NAME)
                and len(s.apps[APP_NAME].units) == NUM_OPENBAO_UNITS
            ),
            timeout=1000,
        )
    root_token, unseal_key = initialize_unseal_authorize_openbao(juju, APP_NAME)
    return OpenBaoInit(root_token, unseal_key)


def test_given_haproxy_deployed_when_integrated_then_status_is_active(
    juju: jubilant.Juju,
    deploy: OpenBaoInit,
):
    external_hostname = "haproxy.example.com"
    juju.config(HAPROXY_APPLICATION_NAME, {"external-hostname": external_hostname})

    juju.integrate(
        f"{SELF_SIGNED_CERTIFICATES_APPLICATION_NAME}:certificates",
        f"{HAPROXY_APPLICATION_NAME}:certificates",
    )

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, HAPROXY_APPLICATION_NAME),
            timeout=SHORT_TIMEOUT,
        )

    juju.integrate(
        f"{APP_NAME}:ingress",
        f"{HAPROXY_APPLICATION_NAME}:ingress",
    )

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, APP_NAME, HAPROXY_APPLICATION_NAME),
            timeout=SHORT_TIMEOUT,
        )
