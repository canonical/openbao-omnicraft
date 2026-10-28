# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Constants shared across the charm."""

from datetime import datetime
from enum import Enum


class Substrate(str, Enum):
    """The kind of cloud the charm is deployed on."""

    K8S = "k8s"
    VM = "vm"


OPENBAO_PORT = 8200
OPENBAO_CLUSTER_PORT = 8201
CONTAINER_NAME = "openbao"
LOG_LEVELS = ("trace", "debug", "info", "warn", "error")

# Relations
AUTOUNSEAL_PROVIDES_RELATION_NAME = "openbao-autounseal-provides"
AUTOUNSEAL_REQUIRES_RELATION_NAME = "openbao-autounseal-requires"
INGRESS_PER_APP_RELATION_NAME = "ingress"
INGRESS_PER_UNIT_RELATION_NAME = "ingress-per-unit"
KV_RELATION_NAME = "openbao-kv"
LOG_FORWARDING_RELATION_NAME = "logging"
PEER_RELATION_NAME = "openbao-peers"
PKI_RELATION_NAME = "openbao-pki"
S3_RELATION_NAME = "s3-parameters"
SEND_CA_CERT_RELATION_NAME = "send-ca-cert"
TLS_CERTIFICATE_ACCESS_RELATION_NAME = "tls-certificates-access"
TLS_CERTIFICATES_ACME_RELATION_NAME = "tls-certificates-acme"
TLS_CERTIFICATES_PKI_RELATION_NAME = "tls-certificates-pki"

# Juju secrets, config and resources
CA_CERTIFICATE_JUJU_SECRET_LABEL = "self-signed-openbao-ca-certificate"
OPENBAO_CHARM_APPROLE_SECRET_LABEL = "openbao-approle-auth-details"
HSM_CONFIG_SECRET_ID_CONFIG_KEY = "hsm-config-secret-id"
HSM_LIB_RESOURCE_NAME = "hsm-lib"
RESOURCE_CONFIG_KEYS = ("cpu-limit", "memory-limit", "cpu-request", "memory-request")

# OpenBao objects managed by the charm
ACME_MOUNT = "charm-acme"
ACME_ROLE_NAME = "charm-acme"
APPROLE_ROLE_NAME = "charm"
AUTOUNSEAL_MOUNT_PATH = "charm-autounseal"
CHARM_POLICY_NAME = "charm-access"
DEFAULT_POLICY_NAME = "default"
PKI_MOUNT = "charm-pki"
PKI_ROLE_NAME = "charm-pki"
PKI_SELF_SIGNED_CA_DEFAULT_VALIDITY_HOURS = 87600

# Files shipped with the charm
CHARM_POLICY_PATH = "src/templates/charm_policy.hcl"
CONFIG_TEMPLATE_DIR_PATH = "src/templates/"
CONFIG_TEMPLATE_NAME = "openbao.hcl.j2"
GRAFANA_DASHBOARDS_PATH = "./src/grafana_dashboards"
PROMETHEUS_ALERT_RULES_PATH = "./src/prometheus_alert_rules"


class Naming:
    """Computes names for OpenBao features.

    This class is used to compute names for OpenBao features based on the charm's
    conventions, such as the key name, policy name, and approle name.  It
    provides a central place to manage them.
    """

    autounseal_approle_prefix: str = "charm-autounseal-"
    autounseal_key_prefix: str = ""
    autounseal_policy_prefix: str = "charm-autounseal-"
    backup_s3_key_prefix: str = "openbao-backup-"
    kv_mount_prefix: str = "charm-"
    kv_secret_prefix: str = "openbao-kv-"

    @classmethod
    def autounseal_key_name(cls, relation_id: int) -> str:
        """Return the key name for the relation."""
        return f"{cls.autounseal_key_prefix}{relation_id}"

    @classmethod
    def autounseal_policy_name(cls, relation_id: int) -> str:
        """Return the policy name for the relation."""
        return f"{cls.autounseal_policy_prefix}{relation_id}"

    @classmethod
    def autounseal_approle_name(cls, relation_id: int) -> str:
        """Return the approle name for the relation."""
        return f"{cls.autounseal_approle_prefix}{relation_id}"

    @classmethod
    def backup_s3_key_name(cls, model_name: str) -> str:
        """Return the key name for the S3 backend."""
        timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        return f"{cls.backup_s3_key_prefix}{model_name}-{timestamp}"

    @classmethod
    def kv_secret_label(cls, unit_name: str) -> str:
        """Return the secret label for the KV backend."""
        unit_name_dash = unit_name.replace("/", "-")
        return f"{cls.kv_secret_prefix}{unit_name_dash}"

    @classmethod
    def kv_mount_path(cls, app_name: str, mount_suffix: str) -> str:
        """Return the mount path for the KV backend."""
        return f"{cls.kv_mount_prefix}{app_name}-{mount_suffix}"

    @classmethod
    def kv_policy_name(cls, mount_path: str, unit_name: str) -> str:
        """Return the policy name for the KV backend."""
        unit_name_dash = unit_name.replace("/", "-")
        return f"{mount_path}-{unit_name_dash}"

    @classmethod
    def kv_role_name(cls, mount_path: str, unit_name: str) -> str:
        """Return the role name for the KV backend."""
        unit_name_dash = unit_name.replace("/", "-")
        return f"{mount_path}-{unit_name_dash}"
