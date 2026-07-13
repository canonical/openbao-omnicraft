import logging
from collections import namedtuple
from pathlib import Path

import jubilant
import pytest

from config import (
    APP_NAME,
    JUJU_FAST_INTERVAL,
    MINIO_S3_ACCESS_KEY,
    MINIO_S3_SECRET_KEY,
    NUM_OPENBAO_UNITS,
    S3_INTEGRATOR_APPLICATION_NAME,
    S3_INTEGRATOR_CHANNEL,
    S3_INTEGRATOR_REVISION,
)
from helpers import (
    configure_s3_and_create_backup,
    deploy_openbao,
    fast_forward,
    get_openbao_token_and_unseal_key,
    initialize_unseal_authorize_openbao,
    list_backups,
    restore_backup,
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
        S3_INTEGRATOR_APPLICATION_NAME,
        S3_INTEGRATOR_APPLICATION_NAME,
        channel=S3_INTEGRATOR_CHANNEL,
        revision=S3_INTEGRATOR_REVISION,
        trust=True,
    )

    # When waiting for OpenBao to go to the blocked state, we may need an update
    # status event to recognize that the API is available, so we wait in
    # fast-forward.
    with fast_forward(juju, JUJU_FAST_INTERVAL):
        juju.wait(
            lambda s: (
                all(
                    u.juju_status.current == "idle"
                    for u in s.apps[S3_INTEGRATOR_APPLICATION_NAME].units.values()
                )
                and jubilant.all_blocked(s, APP_NAME)
                and len(s.apps[APP_NAME].units) == NUM_OPENBAO_UNITS
            ),
            timeout=1000,
        )
    root_token, unseal_key = initialize_unseal_authorize_openbao(juju, APP_NAME)
    return OpenBaoInit(root_token, unseal_key)


@pytest.mark.abort_on_fail
def test_given_openbao_integrated_with_s3_when_create_backup_then_action_succeeds(
    juju: jubilant.Juju,
    deploy: OpenBaoInit,
    host_ip: str,
):
    configure_s3_and_create_backup(
        juju,
        root_token=deploy.root_token,
        s3_endpoint=f"https://{host_ip}:8555",
        s3_access_key=MINIO_S3_ACCESS_KEY,
        s3_secret_key=MINIO_S3_SECRET_KEY,
        s3_bucket="openbao-integration-test",
        s3_region="local",
        kv_secret_value="value",
    )


@pytest.mark.abort_on_fail
def test_given_openbao_integrated_with_s3_when_list_backups_then_action_succeeds(
    juju: jubilant.Juju, deploy: OpenBaoInit
):
    list_backups(juju)


@pytest.mark.abort_on_fail
def test_given_openbao_integrated_with_s3_when_restore_backup_then_action_succeeds(
    juju: jubilant.Juju,
    deploy: OpenBaoInit,
):
    restore_backup(
        juju,
        root_token=deploy.root_token,
        kv_secret_value="value",
    )
