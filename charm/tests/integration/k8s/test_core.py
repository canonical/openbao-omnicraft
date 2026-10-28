# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
from collections import namedtuple
from pathlib import Path

import jubilant
import pytest

from config import (
    APPLICATION_NAME,
    DEPLOY_TIMEOUT,
    JUJU_FAST_INTERVAL,
    NUM_OPENBAO_UNITS,
    SELF_SIGNED_CERTIFICATES_APPLICATION_NAME,
    SELF_SIGNED_CERTIFICATES_CHANNEL,
    SELF_SIGNED_CERTIFICATES_REVISION,
    SHORT_TIMEOUT,
)
from helpers import (
    _get_arch,
    authorize_charm_and_wait,
    crash_pod,
    deploy_openbao,
    fast_forward,
    get_leader_unit_name,
    get_openbao_ca_certificate,
    get_openbao_client,
    get_openbao_token_and_unseal_key,
    get_unit_status_messages,
    initialize_openbao_leader,
    scale,
    unseal_all_openbao_units,
    wait_for_status_message,
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
    deploy_openbao(juju, charm_path=openbao_charm_path, num_units=NUM_OPENBAO_UNITS)

    juju.wait(
        lambda s: (
            jubilant.all_blocked(s, APPLICATION_NAME)
            and len(s.apps[APPLICATION_NAME].units) >= NUM_OPENBAO_UNITS
        ),
        timeout=DEPLOY_TIMEOUT,
    )

    root_token, unseal_key = initialize_openbao_leader(juju, APPLICATION_NAME)
    return OpenBaoInit(root_token, unseal_key)


@pytest.mark.abort_on_fail
def test_given_openbao_deployed_and_initialized_when_unsealed_and_authorized_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    leader_name = get_leader_unit_name(juju, APPLICATION_NAME)
    openbao = get_openbao_client(juju, leader_name, deploy.root_token)
    assert openbao.is_sealed()
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        unseal_all_openbao_units(juju, deploy.unseal_key, deploy.root_token)
        authorize_charm_and_wait(juju, deploy.root_token)
    openbao.wait_for_raft_nodes(expected_num_nodes=NUM_OPENBAO_UNITS)


@pytest.mark.abort_on_fail
def test_given_application_is_deployed_when_pod_crashes_then_unit_recovers(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    k8s_namespace = juju.model
    assert k8s_namespace is not None
    crashing_pod_index = 1
    crashed_unit_name = f"{APPLICATION_NAME}/{crashing_pod_index}"
    crashed_pod_name = f"{APPLICATION_NAME}-{crashing_pod_index}"

    crash_pod(name=crashed_pod_name, namespace=k8s_namespace)
    wait_for_status_message(
        juju,
        expected_message="Please unseal OpenBao",
        timeout=300,
        unit_name=crashed_unit_name,
    )
    openbao = get_openbao_client(juju, crashed_unit_name, deploy.root_token)
    openbao.unseal(deploy.unseal_key)
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APPLICATION_NAME)
                and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
            ),
            timeout=1200,
        )


@pytest.mark.abort_on_fail
def test_given_application_is_deployed_when_scale_up_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    num_units = NUM_OPENBAO_UNITS + 1
    scale(juju, APPLICATION_NAME, num_units)

    wait_for_status_message(juju, expected_message="Please unseal OpenBao", timeout=300, count=1)
    sealed = [
        unit_name
        for unit_name, status in get_unit_status_messages(juju)
        if status == "Please unseal OpenBao"
    ]
    assert len(sealed) == 1
    openbao = get_openbao_client(juju, sealed[0], deploy.root_token)
    openbao.unseal(deploy.unseal_key)

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APPLICATION_NAME)
                and len(s.apps[APPLICATION_NAME].units) == num_units
            ),
            timeout=DEPLOY_TIMEOUT,
        )


@pytest.mark.abort_on_fail
def test_given_application_is_deployed_when_scale_down_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    last_unit_name = list(juju.status().apps[APPLICATION_NAME].units.keys())[-1]
    openbao = get_openbao_client(juju, last_unit_name, deploy.root_token)

    assert openbao.number_of_raft_nodes() == NUM_OPENBAO_UNITS + 1

    scale(juju, APPLICATION_NAME, NUM_OPENBAO_UNITS)
    juju.wait(
        lambda s: (
            jubilant.all_active(s, APPLICATION_NAME)
            and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
        ),
        timeout=SHORT_TIMEOUT,
    )

    first_unit_name = list(juju.status().apps[APPLICATION_NAME].units.keys())[0]
    openbao = get_openbao_client(juju, first_unit_name, deploy.root_token)
    assert openbao.number_of_raft_nodes() == NUM_OPENBAO_UNITS


@pytest.mark.abort_on_fail
def test_given_application_is_deployed_when_apply_k8s_resource_patch_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    juju.config(
        APPLICATION_NAME,
        {
            "cpu-request": "0.75",
            "memory-request": "1Gi",
            "cpu-limit": "2",
            "memory-limit": "2Gi",
        },
    )
    juju.wait(
        lambda s: (
            jubilant.all_blocked(s, APPLICATION_NAME)
            and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
        ),
        timeout=DEPLOY_TIMEOUT,
    )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        unseal_all_openbao_units(juju, deploy.unseal_key, deploy.root_token)
        authorize_charm_and_wait(juju, deploy.root_token)

    juju.wait(
        lambda s: (
            jubilant.all_active(s, APPLICATION_NAME)
            and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
        ),
        timeout=DEPLOY_TIMEOUT,
    )


@pytest.mark.abort_on_fail
def test_given_application_is_deployed_when_self_signed_certificates_integrated_then_status_is_active(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    juju.deploy(
        SELF_SIGNED_CERTIFICATES_APPLICATION_NAME,
        channel=SELF_SIGNED_CERTIFICATES_CHANNEL,
        revision=SELF_SIGNED_CERTIFICATES_REVISION,
        constraints={"arch": _get_arch()},
    )
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_active(s, SELF_SIGNED_CERTIFICATES_APPLICATION_NAME),
            timeout=DEPLOY_TIMEOUT,
        )

    leader_name = get_leader_unit_name(juju, APPLICATION_NAME)
    initial_ca_cert = get_openbao_ca_certificate(juju, leader_name)

    juju.integrate(
        f"{APPLICATION_NAME}:tls-certificates-access",
        f"{SELF_SIGNED_CERTIFICATES_APPLICATION_NAME}:certificates",
    )

    # Integrating the TLS access relation replaces the self-signed certificate
    # OpenBao generated for itself with the one issued by the provider. Swapping
    # the cert reseals OpenBao, so the units go to blocked until they are unsealed.
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                jubilant.all_blocked(s, APPLICATION_NAME)
                and jubilant.all_active(s, SELF_SIGNED_CERTIFICATES_APPLICATION_NAME)
            ),
            timeout=SHORT_TIMEOUT,
        )

    final_ca_cert = get_openbao_ca_certificate(juju, leader_name)
    assert initial_ca_cert != final_ca_cert

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        unseal_all_openbao_units(juju, deploy.unseal_key, deploy.root_token)
        juju.wait(
            lambda s: jubilant.all_active(s, APPLICATION_NAME),
            timeout=SHORT_TIMEOUT,
        )


@pytest.mark.abort_on_fail
def test_given_tls_certificates_integrated_when_openbao_ca_certificate_is_returned_then_ca_cert_is_valid(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    leader_name = get_leader_unit_name(juju, APPLICATION_NAME)
    ca_cert = get_openbao_ca_certificate(juju, leader_name)
    assert ca_cert


@pytest.mark.abort_on_fail
def test_given_tls_certificates_integrated_when_openbao_unit_crashes_then_openbao_uses_tls(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    k8s_namespace = juju.model
    assert k8s_namespace is not None
    crashing_pod_index = 1
    crashed_unit_name = f"{APPLICATION_NAME}/{crashing_pod_index}"
    crashed_pod_name = f"{APPLICATION_NAME}-{crashing_pod_index}"

    crash_pod(name=crashed_pod_name, namespace=k8s_namespace)
    wait_for_status_message(
        juju,
        expected_message="Please unseal OpenBao",
        timeout=300,
        unit_name=crashed_unit_name,
    )

    # After the crash + TLS cert re-delivery, multiple units may need
    # unsealing (TLS reconfiguration can restart OpenBao on all units).
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        unseal_all_openbao_units(juju, deploy.unseal_key, deploy.root_token)
        juju.wait(
            lambda s: (
                jubilant.all_active(s, APPLICATION_NAME)
                and len(s.apps[APPLICATION_NAME].units) == NUM_OPENBAO_UNITS
            ),
            timeout=1200,
        )

    # The recovered unit must serve OpenBao over the provider-issued TLS CA,
    # i.e. the same CA as the rest of the cluster, rather than a self-signed one.
    leader_name = get_leader_unit_name(juju, APPLICATION_NAME)
    leader_ca_cert = get_openbao_ca_certificate(juju, leader_name)
    recovered_ca_cert = get_openbao_ca_certificate(juju, crashed_unit_name)
    assert recovered_ca_cert
    assert recovered_ca_cert == leader_ca_cert


@pytest.mark.abort_on_fail
def test_given_tls_access_relation_destroyed_then_self_signed_cert_created(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    leader_name = get_leader_unit_name(juju, APPLICATION_NAME)
    initial_ca_cert = get_openbao_ca_certificate(juju, leader_name)

    juju.remove_relation(
        f"{APPLICATION_NAME}:tls-certificates-access",
        f"{SELF_SIGNED_CERTIFICATES_APPLICATION_NAME}:certificates",
    )

    # Removing the relation makes OpenBao fall back to generating its own
    # self-signed certificate. Regenerating the cert reseals OpenBao, so the units
    # go to blocked until they are unsealed again.
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: jubilant.all_blocked(s, APPLICATION_NAME),
            timeout=SHORT_TIMEOUT,
        )

    final_ca_cert = get_openbao_ca_certificate(juju, leader_name)
    assert initial_ca_cert != final_ca_cert

    with fast_forward(juju, JUJU_FAST_INTERVAL):
        unseal_all_openbao_units(juju, deploy.unseal_key, deploy.root_token)
        juju.wait(
            lambda s: jubilant.all_active(s, APPLICATION_NAME),
            timeout=SHORT_TIMEOUT,
        )
