# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
from collections import namedtuple
from pathlib import Path

import jubilant
import pytest

from config import (
    APPLICATION_NAME,
    JUJU_FAST_INTERVAL,
    NUM_OPENBAO_UNITS,
    OPENBAO_KV_REQUIRER_1_APPLICATION_NAME,
    OPENBAO_KV_REQUIRER_2_APPLICATION_NAME,
    SHORT_TIMEOUT,
)
from helpers import (
    crash_pod,
    deploy_openbao,
    fast_forward,
    get_openbao_token_and_unseal_key,
    initialize_unseal_authorize_openbao,
)

logger = logging.getLogger(__name__)

OpenBaoInit = namedtuple("OpenBaoInit", ["root_token", "unseal_key"])


@pytest.fixture(scope="module")
def deploy(
    juju: jubilant.Juju, openbao_charm_path: Path, kv_requirer_charm_path: Path, skip_deploy: bool
) -> OpenBaoInit:
    """Build and deploy the application."""
    if skip_deploy:
        logger.info("Skipping deployment due to --no-deploy flag")
        root_token, key = get_openbao_token_and_unseal_key(juju, APPLICATION_NAME)
        return OpenBaoInit(root_token, key)
    deploy_openbao(juju, charm_path=openbao_charm_path, num_units=NUM_OPENBAO_UNITS)
    juju.deploy(kv_requirer_charm_path, OPENBAO_KV_REQUIRER_1_APPLICATION_NAME)

    juju.wait(
        lambda s: (
            jubilant.all_blocked(s, APPLICATION_NAME)
            and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
            and jubilant.all_active(s, OPENBAO_KV_REQUIRER_1_APPLICATION_NAME)
        ),
    )
    root_token, unseal_key = initialize_unseal_authorize_openbao(juju, APPLICATION_NAME)
    return OpenBaoInit(root_token, unseal_key)


@pytest.mark.abort_on_fail
def test_given_openbao_kv_requirer_deployed_when_openbao_kv_relation_created_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    juju.integrate(
        f"{APPLICATION_NAME}:openbao-kv",
        f"{OPENBAO_KV_REQUIRER_1_APPLICATION_NAME}:openbao-kv",
    )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APPLICATION_NAME, OPENBAO_KV_REQUIRER_1_APPLICATION_NAME)
                and all(
                    u.juju_status.current == "idle"
                    for app in [APPLICATION_NAME, OPENBAO_KV_REQUIRER_1_APPLICATION_NAME]
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

    juju.run(
        f"{OPENBAO_KV_REQUIRER_1_APPLICATION_NAME}/0",
        "create-secret",
        {"key": secret_key, "value": secret_value},
        wait=30,
    )

    task = juju.run(
        f"{OPENBAO_KV_REQUIRER_1_APPLICATION_NAME}/0",
        "get-secret",
        {"key": secret_key},
        wait=30,
    )

    assert task.results["value"] == secret_value


@pytest.mark.abort_on_fail
def test_given_openbao_kv_requirer_related_and_requirer_pod_crashes_when_create_secret_then_secret_is_created(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    secret_key = "test-key"
    secret_value = "test-value"
    k8s_namespace = juju.model
    assert k8s_namespace is not None

    crash_pod(
        name=f"{OPENBAO_KV_REQUIRER_1_APPLICATION_NAME}-0",
        namespace=k8s_namespace,
    )

    juju.wait(
        lambda s: (
            jubilant.all_active(s, OPENBAO_KV_REQUIRER_1_APPLICATION_NAME)
            and len(s.apps[OPENBAO_KV_REQUIRER_1_APPLICATION_NAME].units) == 1
            and all(
                u.juju_status.current == "idle"
                for u in s.apps[OPENBAO_KV_REQUIRER_1_APPLICATION_NAME].units.values()
            )
        ),
    )

    juju.run(
        f"{OPENBAO_KV_REQUIRER_1_APPLICATION_NAME}/0",
        "create-secret",
        {"key": secret_key, "value": secret_value},
        wait=30,
    )

    task = juju.run(
        f"{OPENBAO_KV_REQUIRER_1_APPLICATION_NAME}/0",
        "get-secret",
        {"key": secret_key},
        wait=30,
    )

    assert task.results["value"] == secret_value


@pytest.mark.abort_on_fail
def test_given_multiple_kv_requirers_related_when_secrets_created_then_secrets_created(
    juju: jubilant.Juju, kv_requirer_charm_path: Path
):
    juju.deploy(kv_requirer_charm_path, OPENBAO_KV_REQUIRER_2_APPLICATION_NAME)
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, OPENBAO_KV_REQUIRER_2_APPLICATION_NAME),
        )
    juju.integrate(
        f"{APPLICATION_NAME}:openbao-kv",
        f"{OPENBAO_KV_REQUIRER_2_APPLICATION_NAME}:openbao-kv",
    )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APPLICATION_NAME, OPENBAO_KV_REQUIRER_2_APPLICATION_NAME)
                and all(
                    u.juju_status.current == "idle"
                    for app in [APPLICATION_NAME, OPENBAO_KV_REQUIRER_2_APPLICATION_NAME]
                    for u in s.apps[app].units.values()
                )
            ),
            timeout=SHORT_TIMEOUT,
        )
    secret_key = "test-key-2"
    secret_value = "test-value-2"

    juju.run(
        f"{OPENBAO_KV_REQUIRER_2_APPLICATION_NAME}/0",
        "create-secret",
        {"key": secret_key, "value": secret_value},
        wait=30,
    )

    task = juju.run(
        f"{OPENBAO_KV_REQUIRER_2_APPLICATION_NAME}/0",
        "get-secret",
        {"key": secret_key},
        wait=30,
    )

    assert task.results["value"] == secret_value
