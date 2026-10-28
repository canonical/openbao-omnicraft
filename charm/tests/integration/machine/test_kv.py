import logging
from collections import namedtuple
from pathlib import Path

import jubilant
import pytest

from config import (
    APP_NAME,
    JUJU_FAST_INTERVAL,
    NUM_OPENBAO_UNITS,
    OPENBAO_KV_REQUIRER_APPLICATION_NAME,
    SELF_SIGNED_CERTIFICATES_APPLICATION_NAME,
    SELF_SIGNED_CERTIFICATES_CHANNEL,
    SELF_SIGNED_CERTIFICATES_REVISION,
    SHORT_TIMEOUT,
)
from helpers import (
    deploy_openbao,
    fast_forward,
    get_openbao_token_and_unseal_key,
    has_relation,
    initialize_unseal_authorize_openbao,
    run_action_on_leader,
)

logger = logging.getLogger(__name__)

OpenBaoInit = namedtuple("OpenBaoInit", ["root_token", "unseal_key"])


@pytest.fixture(scope="module")
def deploy(
    juju: jubilant.Juju, openbao_charm_path: Path, skip_deploy: bool, kv_requirer_charm_path: Path
) -> OpenBaoInit:
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
        channel=SELF_SIGNED_CERTIFICATES_CHANNEL,
        revision=SELF_SIGNED_CERTIFICATES_REVISION,
    )
    juju.deploy(kv_requirer_charm_path, OPENBAO_KV_REQUIRER_APPLICATION_NAME)

    # When waiting for OpenBao to go to the blocked state, we may need an update
    # status event to recognize that the API is available, so we wait in
    # fast-forward.
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, SELF_SIGNED_CERTIFICATES_APPLICATION_NAME)
                and jubilant.all_active(s, OPENBAO_KV_REQUIRER_APPLICATION_NAME)
                and jubilant.all_blocked(s, APP_NAME)
                and len(s.apps[APP_NAME].units) == NUM_OPENBAO_UNITS
            ),
            timeout=1000,
        )
    root_token, unseal_key = initialize_unseal_authorize_openbao(juju, APP_NAME)
    return OpenBaoInit(root_token, unseal_key)


@pytest.mark.abort_on_fail
def test_given_openbao_kv_requirer_deployed_when_openbao_kv_relation_created_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    if not has_relation(juju, APP_NAME, "openbao-kv"):
        juju.integrate(
            f"{APP_NAME}:openbao-kv",
            f"{OPENBAO_KV_REQUIRER_APPLICATION_NAME}:openbao-kv",
        )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APP_NAME, OPENBAO_KV_REQUIRER_APPLICATION_NAME)
                and all(
                    u.juju_status.current == "idle"
                    for app in [APP_NAME, OPENBAO_KV_REQUIRER_APPLICATION_NAME]
                    for u in s.apps[app].units.values()
                )
            ),
            timeout=SHORT_TIMEOUT,
        )


@pytest.mark.abort_on_fail
def test_given_openbao_kv_requirer_related_when_create_secret_then_secret_is_created(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    secret_key = "test-key"
    secret_value = "test-value"
    run_action_on_leader(
        juju,
        OPENBAO_KV_REQUIRER_APPLICATION_NAME,
        action_name="create-secret",
        key=secret_key,
        value=secret_value,
    )

    openbao_kv_get_secret_results = run_action_on_leader(
        juju,
        OPENBAO_KV_REQUIRER_APPLICATION_NAME,
        action_name="get-secret",
        key=secret_key,
    )

    assert openbao_kv_get_secret_results["value"] == secret_value
