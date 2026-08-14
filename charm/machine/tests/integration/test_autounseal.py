import logging
from collections import namedtuple
from pathlib import Path

import jubilant
import pytest

import config
from config import (
    APP_NAME,
    JUJU_FAST_INTERVAL,
    NUM_OPENBAO_UNITS,
    SELF_SIGNED_CERTIFICATES_APPLICATION_NAME,
    SELF_SIGNED_CERTIFICATES_REVISION,
)
from helpers import (
    ActionFailedError,
    authorize_charm,
    deploy_openbao,
    fast_forward,
    get_openbao_token_and_unseal_key,
    initialize_openbao_leader,
    initialize_unseal_authorize_openbao,
    wait_for_status_message,
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

    # When waiting for OpenBao to go to the blocked state, we may need an update
    # status event to recognize that the API is available, so we wait in
    # fast-forward.
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, SELF_SIGNED_CERTIFICATES_APPLICATION_NAME)
                and jubilant.all_blocked(s, APP_NAME)
                and len(s.apps[APP_NAME].units) == NUM_OPENBAO_UNITS
            ),
            timeout=1000,
        )
    root_token, unseal_key = initialize_unseal_authorize_openbao(juju, APP_NAME)
    return OpenBaoInit(root_token, unseal_key)


@pytest.mark.abort_on_fail
def test_given_openbao_is_deployed_when_integrate_another_openbao_then_autounseal_activated(
    juju: jubilant.Juju, deploy: OpenBaoInit, openbao_charm_path: Path
):
    # Arrange
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.deploy(
            openbao_charm_path,
            "openbao-b",
            trust=True,
            num_units=1,
        )
        juju.wait(
            lambda s: (
                "openbao-b" in s.apps
                and jubilant.all_blocked(s, "openbao-b")
                and len(s.apps["openbao-b"].units) == 1
            ),
            timeout=600,
        )

    juju.integrate(
        "openbao-b:tls-certificates-access",
        f"{SELF_SIGNED_CERTIFICATES_APPLICATION_NAME}:certificates",
    )

    # Act
    juju.integrate(
        f"{APP_NAME}:openbao-autounseal-provides", "openbao-b:openbao-autounseal-requires"
    )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                "openbao-b" in s.apps
                and jubilant.all_blocked(s, "openbao-b")
                and len(s.apps["openbao-b"].units) == 1
            ),
            timeout=300,
        )

        wait_for_status_message(
            juju=juju,
            count=1,
            expected_message="Please initialize OpenBao",
            app_name="openbao-b",
        )

        root_token, recovery_key = initialize_openbao_leader(juju, "openbao-b")
        wait_for_status_message(
            juju=juju,
            count=1,
            expected_message="Please authorize charm (see `authorize-charm` action)",
            app_name="openbao-b",
        )
        try:
            authorize_charm(juju, root_token, "openbao-b")
        except ActionFailedError:
            logger.warning("Failed to authorize charm")

    # Assert
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, "openbao-b") and len(s.apps["openbao-b"].units) == 1,
            timeout=300,
        )


@pytest.mark.abort_on_fail
def test_given_openbao_b_is_deployed_and_autounsealed_when_add_unit_then_status_is_active(
    juju: jubilant.Juju,
):
    assert len(juju.status().apps["openbao-b"].units) == 1
    juju.add_unit("openbao-b", num_units=1)
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, "openbao-b") and len(s.apps["openbao-b"].units) == 2,
            timeout=300,
        )
