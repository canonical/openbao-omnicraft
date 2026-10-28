# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
from collections import namedtuple
from pathlib import Path

import jubilant
import pytest

from config import (
    APPLICATION_NAME,
    AUTOUNSEAL_TOKEN_SECRET_LABEL,
    JUJU_FAST_INTERVAL,
    METADATA,
    NUM_OPENBAO_UNITS,
)
from helpers import (
    authorize_charm,
    crash_pod,
    deploy_openbao,
    fast_forward,
    get_leader_unit_name,
    get_model_secret_field,
    get_openbao_token_and_unseal_key,
    get_unit_address,
    initialize_openbao_leader,
    initialize_unseal_authorize_openbao,
    revoke_token,
    scale,
    wait_for_status_message,
)
from openbao_helpers import OpenBao

logger = logging.getLogger(__name__)

OpenBaoInit = namedtuple("OpenBaoInit", ["root_token", "unseal_key"])


@pytest.fixture(scope="module")
def deploy(juju: jubilant.Juju, openbao_charm_path: Path, skip_deploy: bool) -> OpenBaoInit:
    """Build and deploy the application."""
    if skip_deploy:
        logger.info("Skipping deployment due to --no-deploy flag")
        root_token, key = get_openbao_token_and_unseal_key(juju, APPLICATION_NAME)
        return OpenBaoInit(root_token, key)
    resources = {"openbao-image": METADATA["resources"]["openbao-image"]["upstream-source"]}
    juju.deploy(
        openbao_charm_path,
        "openbao-b",
        num_units=1,
        resources=resources,
        trust=True,
    )
    deploy_openbao(
        juju,
        charm_path=openbao_charm_path,
        num_units=NUM_OPENBAO_UNITS,
    )

    juju.wait(
        lambda s: (
            jubilant.all_blocked(s, APPLICATION_NAME)
            and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
            and jubilant.all_blocked(s, "openbao-b")
            and len(s.apps["openbao-b"].units) == 1
        ),
    )

    root_token, unseal_key = initialize_unseal_authorize_openbao(juju, APPLICATION_NAME)
    return OpenBaoInit(root_token, unseal_key)


@pytest.mark.abort_on_fail
def test_given_openbao_is_deployed_when_integrate_another_openbao_then_autounseal_activated(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    juju.integrate(
        f"{APPLICATION_NAME}:openbao-autounseal-provides", "openbao-b:openbao-autounseal-requires"
    )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_blocked(s, "openbao-b") and len(s.apps["openbao-b"].units) == 1,
        )

        wait_for_status_message(
            juju=juju,
            expected_message="Please initialize OpenBao",
            app_name="openbao-b",
        )

        root_token_openbao_b, _ = initialize_openbao_leader(juju, "openbao-b")
        wait_for_status_message(
            juju=juju,
            expected_message="Please authorize charm (see `authorize-charm` action)",
            app_name="openbao-b",
        )
        authorize_charm(juju, root_token_openbao_b, "openbao-b")
        juju.wait(
            lambda s: jubilant.all_active(s, "openbao-b") and len(s.apps["openbao-b"].units) == 1,
        )


@pytest.mark.abort_on_fail
def test_given_openbao_b_is_deployed_and_unsealed_when_scale_up_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        scale(juju, "openbao-b", 1)
        juju.wait(
            lambda s: jubilant.all_active(s, "openbao-b") and len(s.apps["openbao-b"].units) == 1,
        )
        scale(juju, "openbao-b", 3)
        juju.wait(
            lambda s: jubilant.all_active(s, "openbao-b") and len(s.apps["openbao-b"].units) == 3,
        )


@pytest.mark.abort_on_fail
def test_given_openbao_b_is_deployed_and_unsealed_when_all_units_crash_then_units_recover(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, "openbao-b") and len(s.apps["openbao-b"].units) == 3,
        )

    k8s_namespace = juju.model
    assert k8s_namespace is not None
    crash_pod(name="openbao-b-0", namespace=k8s_namespace)
    crash_pod(name="openbao-b-1", namespace=k8s_namespace)
    crash_pod(name="openbao-b-2", namespace=k8s_namespace)
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, "openbao-b") and len(s.apps["openbao-b"].units) == 3,
        )
        leader_unit_name = get_leader_unit_name(juju, "openbao-b")
    leader_unit_address = get_unit_address(juju, leader_unit_name)
    root_token_openbao_b, _ = get_openbao_token_and_unseal_key(juju, "openbao-b")
    openbao = OpenBao(
        url=f"https://{leader_unit_address}:8200",
        token=root_token_openbao_b,
    )
    openbao.wait_for_raft_nodes(expected_num_nodes=NUM_OPENBAO_UNITS)


@pytest.mark.abort_on_fail
def test_given_openbao_b_is_deployed_and_unsealed_when_auth_token_goes_bad_then_units_recover(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, "openbao-b") and len(s.apps["openbao-b"].units) == 3,
        )
    auth_token = get_model_secret_field(
        juju=juju, label=AUTOUNSEAL_TOKEN_SECRET_LABEL, field="token"
    )
    leader_unit_name = get_leader_unit_name(juju, "openbao-b")
    leader_unit_address = get_unit_address(juju, leader_unit_name)
    root_token_openbao_b, _ = get_openbao_token_and_unseal_key(juju, "openbao-b")

    revoke_token(
        token_to_revoke=auth_token,
        root_token=root_token_openbao_b,
        endpoint=leader_unit_address,
    )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, "openbao-b") and len(s.apps["openbao-b"].units) == 3,
        )
