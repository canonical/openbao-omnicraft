import platform
from pathlib import Path

import yaml

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())

# Path to the built openbao snap, set from the --snap_path pytest option in conftest.
OPENBAO_SNAP_PATH: str | None = None
# Local SoftHSM snap (.snap) for PKCS#11 tests when the store package is unavailable.
SOFTHSM_SNAP_PATH: str | None = None

APP_NAME: str = METADATA["name"]
GRAFANA_AGENT_APPLICATION_NAME = "grafana-agent"
GRAFANA_AGENT_CHANNEL = "1/stable"
# Charm revisions are published per architecture.
GRAFANA_AGENT_REVISION = {"x86_64": 605, "aarch64": 606}.get(platform.machine(), 605)
HAPROXY_APPLICATION_NAME = "haproxy"
HAPROXY_REVISION = 290
INGRESS_RELATION_NAME = "ingress"
MINIO_APPLICATION_NAME = "minio"
MINIO_S3_ACCESS_KEY = "baointegrationtest"
MINIO_S3_SECRET_KEY = "baointegrationtest"

MICROCEPH_S3_ACCESS_KEY = "openbaomicrocephtest"
MICROCEPH_S3_SECRET_KEY = "openbaomicrocephtest"
MICROCEPH_S3_BUCKET = "openbao-microceph-test"
MICROCEPH_RGW_PORT = 7480
NUM_OPENBAO_UNITS = 3
PEER_RELATION_NAME = "openbao-peers"
S3_INTEGRATOR_APPLICATION_NAME = "s3-integrator"
S3_INTEGRATOR_CHANNEL = "stable"
S3_INTEGRATOR_REVISION = 146
SELF_SIGNED_CERTIFICATES_APPLICATION_NAME = "self-signed-certificates"
SELF_SIGNED_CERTIFICATES_CHANNEL = "1/stable"
SELF_SIGNED_CERTIFICATES_REVISION = 317
OPENBAO_KV_REQUIRER_APPLICATION_NAME = "openbao-kv-requirer"
OPENBAO_PKI_REQUIRER_APPLICATION_NAME = "tls-certificates-requirer"
OPENBAO_PKI_REQUIRER_CHANNEL = "latest/stable"

OPENBAO_KV_REQUIRER_CHARM_DIR = "tests/integration/openbao_kv_requirer_operator"

MATCHING_COMMON_NAME = "example.com"
UNMATCHING_COMMON_NAME = "unmatching-the-requirer.com"
OPENBAO_PKI_REQUIRER_REVISION = 93

# There is a dependency here on the `idle_period` we use in `wait_for_idle()`.
# This value should be greater than the `idle_period` used, otherwise the
# `wait_for_idle` function may catch the charm executing the `update-status`
# hook and reset the timer. `idle_period` default is 15s.
JUJU_FAST_INTERVAL = "20s"

# How long to wait for apps to settle after integrating them, or configuring them. These events should be quick.
SHORT_TIMEOUT = 60 * 2
REFRESH_TIMEOUT = 60 * 10
