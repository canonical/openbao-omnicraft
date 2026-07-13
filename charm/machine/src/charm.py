#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.


"""A machine charm for OpenBao."""

import json
import logging
import socket
import subprocess
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from charmlibs.interfaces.tls_certificates import (
    CertificateRequestAttributes,
    Mode,
    TLSCertificatesProvidesV4,
    TLSCertificatesRequiresV4,
)
from charms.data_platform_libs.v0.s3 import S3Requirer
from charms.grafana_agent.v0.cos_agent import COSAgentProvider
from charms.openbao_k8s.v0.openbao_kv import OpenBaoKvClientDetachedEvent, OpenBaoKvProvides
from charms.operator_libs_linux.v2 import snap
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer
from jinja2 import Environment, FileSystemLoader
from openbao.juju_facade import (
    JujuFacade,
    NoSuchSecretError,
    SecretRemovedError,
    TransientJujuError,
)
from openbao.openbao_autounseal import OpenBaoAutounsealProvides, OpenBaoAutounsealRequires
from openbao.openbao_client import (
    AppRole,
    OpenBaoAuthenticationError,
    OpenBaoClient,
    OpenBaoClientError,
    SecretsBackend,
    Token,
)
from openbao.openbao_helpers import (
    AutounsealConfiguration,
    allowed_domains_config_is_valid,
    common_name_config_is_valid,
    config_file_content_matches,
    get_env_var,
    render_openbao_config_file,
    sans_dns_config_is_valid,
    sans_ip_config_is_valid,
)
from openbao.openbao_managers import (
    TLS_CERTIFICATE_ACCESS_RELATION_NAME,
    TLS_CERTIFICATES_ACME_RELATION_NAME,
    ACMEManager,
    AutounsealProviderManager,
    AutounsealRequirerManager,
    BackupManager,
    File,
    KVManager,
    ManagerError,
    OpenBaoCertsError,
    PKIManager,
    RaftManager,
    TLSManager,
)
from ops import ActionEvent, BlockedStatus, ErrorStatus
from ops.charm import CharmBase, CollectStatusEvent, RemoveEvent
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus, ModelError, Relation, WaitingStatus

from machine import Machine
from systemd_creds import SystemdCreds

logger = logging.getLogger(__name__)

ACME_MOUNT = "charm-acme"
ACME_ROLE_NAME = "charm-acme"
AUTOUNSEAL_MOUNT_PATH = "charm-autounseal"
AUTOUNSEAL_PROVIDES_RELATION_NAME = "openbao-autounseal-provides"
AUTOUNSEAL_REQUIRES_RELATION_NAME = "openbao-autounseal-requires"
BACKUP_KEY_PREFIX = "openbao-backup"
CONFIG_TEMPLATE_NAME = "openbao.hcl.j2"
INGRESS_RELATION_NAME = "ingress"
KV_RELATION_NAME = "openbao-kv"
KV_SECRET_PREFIX = "kv-creds-"
LOGROTATE_PATH = Path("/etc/logrotate.d/rsyslog")
LOGROTATE_DEFAULT_COUNT = 7
LOGROTATE_DEFAULT_MAXSIZE = "10M"
MACHINE_TLS_FILE_DIRECTORY_PATH = "/var/snap/openbao/common/certs"
METRICS_ALERT_RULES_PATH = "./src/prometheus_alert_rules"
PEER_RELATION_NAME = "openbao-peers"
PKI_RELATION_NAME = "openbao-pki"
REQUIRED_S3_PARAMETERS = ["bucket", "access-key", "secret-key", "endpoint"]
S3_RELATION_NAME = "s3-parameters"
SYSTEMD_DROP_IN_DIR = "/etc/systemd/system/snap.openbao.baod.service.d"
SYSTEMD_DROP_IN_FILE_PATH = f"{SYSTEMD_DROP_IN_DIR}/10-charm.conf"
SYSTEMD_CRED_EXTERNAL_BAO_TOKEN_NAME = "external_openbao_token"
TEMPLATE_PATH = "src/templates/"
TEMPLATE_SYSTEMD_DROP_IN_CREDS = "systemd_dropin_creds.conf.j2"
TEMPLATE_OPENBAO_ENV_LOAD_SYSTEMD_CREDS = "openbao_load_systemd_creds.env.j2"
TLS_CERTIFICATES_PKI_RELATION_NAME = "tls-certificates-pki"
OPENBAO_CHARM_APPROLE_SECRET_LABEL = "openbao-approle-auth-details"
OPENBAO_CHARM_POLICY_NAME = "charm-access"
OPENBAO_CHARM_POLICY_PATH = "src/templates/charm_policy.hcl"
OPENBAO_CLUSTER_PORT = 8201
# The config file name must match what the snap's baod-start script reads
# from $SNAP_COMMON.
OPENBAO_CONFIG_FILE_NAME = "openbao-config.hcl"
OPENBAO_CONFIG_PATH = "/var/snap/openbao/common"
OPENBAO_ENV_PATH = f"{OPENBAO_CONFIG_PATH}/openbao.env"
OPENBAO_DEFAULT_POLICY_NAME = "default"
OPENBAO_PKI_MOUNT = "charm-pki"
OPENBAO_PKI_ROLE = "charm-pki"
OPENBAO_PORT = 8200
OPENBAO_SNAP_NAME = "openbao"
OPENBAO_SNAP_RESOURCE_NAME = "openbao-snap"
OPENBAO_SNAP_SERVICE_NAME = "baod"
# Name of the workload process as it appears in the process table.
OPENBAO_PROCESS_NAME = "bao"
OPENBAO_STORAGE_PATH = "/var/snap/openbao/common/raft"


class OpenBaoOperatorCharm(CharmBase):
    """Machine Charm for OpenBao."""

    def __init__(self, *args: Any):
        super().__init__(*args)
        self.juju_facade = JujuFacade(self)
        self.juju_facade.set_unit_ports(OPENBAO_PORT)
        self.machine = Machine()
        self._cos_agent = COSAgentProvider(
            self,
            refresh_events=[
                self.on[PEER_RELATION_NAME].relation_changed,
            ],
            scrape_configs=self.generate_openbao_scrape_configs,
            dashboard_dirs=["./src/grafana_dashboards"],
            metrics_rules_dir=METRICS_ALERT_RULES_PATH,
        )
        access_sans_dns = self.juju_facade.get_string_config("access_sans_dns")
        access_sans_dns_list = [socket.getfqdn()]
        if access_sans_dns:
            if not sans_dns_config_is_valid(access_sans_dns):
                logger.warning("access_sans_dns is not valid, it must be a comma separated list")
                access_sans_dns_list = []
            else:
                access_sans_dns_list.extend([name.strip() for name in access_sans_dns.split(",")])
        ip_addresses = set()
        if self._bind_address:
            ip_addresses.add(self._bind_address)
        ingress_addresses = self.juju_facade.get_ingress_addresses(PEER_RELATION_NAME)
        ip_addresses.update(ingress_addresses)
        access_sans_ip = self.juju_facade.get_string_config("access_sans_ip")
        if access_sans_ip:
            if not sans_ip_config_is_valid(access_sans_ip):
                logger.warning(
                    "access_sans_ip is not valid, it must be a comma separated list of IP addresses"
                )
            else:
                ip_addresses.update([ip.strip() for ip in access_sans_ip.split(",")])
        self.tls = TLSManager(
            charm=self,
            workload=self.machine,
            service_name=OPENBAO_SNAP_NAME,
            tls_directory_path=MACHINE_TLS_FILE_DIRECTORY_PATH,
            common_name=self._bind_address if self._bind_address else "",
            sans_dns=frozenset(access_sans_dns_list),
            sans_ip=frozenset(ip_addresses),
            country_name=self.juju_facade.get_string_config("access_country_name"),
            state_or_province_name=self.juju_facade.get_string_config(
                "access_state_or_province_name"
            ),
            locality_name=self.juju_facade.get_string_config("access_locality_name"),
            organization=self.juju_facade.get_string_config("access_organization"),
            organizational_unit=self.juju_facade.get_string_config("access_organizational_unit"),
            email_address=self.juju_facade.get_string_config("access_email_address"),
        )
        self.openbao_kv = OpenBaoKvProvides(self, KV_RELATION_NAME)
        self.openbao_pki = TLSCertificatesProvidesV4(
            charm=self,
            relationship_name=PKI_RELATION_NAME,
        )
        self.ingress = IngressPerAppRequirer(
            charm=self,
            relation_name=INGRESS_RELATION_NAME,
            port=OPENBAO_PORT,
            strip_prefix=True,
            scheme=lambda: "https",
            redirect_https=True,
        )
        pki_certificate_request = self._get_pki_certificate_request()
        self.tls_certificates_pki = TLSCertificatesRequiresV4(
            charm=self,
            relationship_name=TLS_CERTIFICATES_PKI_RELATION_NAME,
            certificate_requests=[pki_certificate_request] if pki_certificate_request else [],
            mode=Mode.APP,
            refresh_events=[self.on.config_changed],
        )
        acme_certificate_request = self._get_acme_certificate_request()
        self.tls_certificates_acme = TLSCertificatesRequiresV4(
            charm=self,
            relationship_name=TLS_CERTIFICATES_ACME_RELATION_NAME,
            certificate_requests=[acme_certificate_request] if acme_certificate_request else [],
            mode=Mode.APP,
            refresh_events=[self.on.config_changed],
        )
        self.s3_requirer = S3Requirer(self, S3_RELATION_NAME)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self.framework.observe(self.on.remove, self._on_remove)
        self.openbao_autounseal_provides = OpenBaoAutounsealProvides(
            self, AUTOUNSEAL_PROVIDES_RELATION_NAME
        )
        self.openbao_autounseal_requires = OpenBaoAutounsealRequires(
            self, AUTOUNSEAL_REQUIRES_RELATION_NAME
        )
        configure_events = [
            self.on.config_changed,
            self.on[PEER_RELATION_NAME].relation_created,
            self.on[PEER_RELATION_NAME].relation_changed,
            self.on.install,
            self.on.update_status,
            self.openbao_autounseal_provides.on.openbao_autounseal_requirer_relation_broken,
            self.openbao_autounseal_requires.on.openbao_autounseal_details_ready,
            self.openbao_autounseal_provides.on.openbao_autounseal_requirer_relation_created,
            self.openbao_autounseal_requires.on.openbao_autounseal_provider_relation_broken,
            self.tls_certificates_pki.on.certificate_available,
            self.on.tls_certificates_pki_relation_joined,
            self.on.openbao_pki_relation_changed,
            self.openbao_kv.on.new_openbao_kv_client_attached,
        ]
        for event in configure_events:
            self.framework.observe(event, self._configure)
        self.framework.observe(
            self.openbao_kv.on.openbao_kv_client_detached, self._on_openbao_kv_client_detached
        )

        # Actions
        self.framework.observe(self.on.authorize_charm_action, self._on_authorize_charm_action)
        self.framework.observe(self.on.bootstrap_raft_action, self._on_bootstrap_raft_action)
        self.framework.observe(self.on.create_backup_action, self._on_create_backup_action)
        self.framework.observe(self.on.list_backups_action, self._on_list_backups_action)
        self.framework.observe(self.on.restore_backup_action, self._on_restore_backup_action)

    def _on_openbao_kv_client_detached(self, event: OpenBaoKvClientDetachedEvent):
        KVManager.remove_unit_credentials(self.juju_facade, event.unit_name)

    def _get_active_openbao_client(self) -> OpenBaoClient | None:
        """Return a client for the _active_ openbao service.

        This may not be the OpenBao service running on this unit.
        """
        addresses = self._get_peer_relation_node_api_addresses()
        for address in addresses:
            try:
                openbao = OpenBaoClient(
                    address, ca_cert_path=self.tls.get_tls_file_path_in_charm(File.CA)
                )
            except OpenBaoCertsError as e:
                logger.warning("Failed to get OpenBao client: %s", e)
                continue
            if openbao.is_active():
                if not openbao.is_api_available():
                    return None
                if not (approle := self._get_openbao_approle_secret()):
                    return None
                try:
                    if not openbao.authenticate(approle):
                        return None
                except OpenBaoAuthenticationError:
                    logger.warning(
                        "OpenBao rejected the charm's credentials. "
                        "Removing stored credentials to prevent lockout."
                    )
                    self.juju_facade.remove_secret(label=OPENBAO_CHARM_APPROLE_SECRET_LABEL)
                    return None
                return openbao
        return None

    def _get_authenticated_openbao_client(self) -> OpenBaoClient | None:
        """Return an authenticate openbao client.

        Returns:
            OpenBao: An active OpenBao client configured with the cluster address
                   and CA certificate, and authorized with the AppRole
                   credentials set upon initial authorization of the charm, or
                   `None` if the client could not be successfully created or
                   has not been authorized.
        """
        openbao = self._get_openbao_client()
        if not openbao:
            return None
        if not openbao.is_api_available():
            return None
        approle = self._get_openbao_approle_secret()
        if not approle:
            return None
        try:
            if not openbao.authenticate(approle):
                return None
        except OpenBaoAuthenticationError:
            logger.warning(
                "OpenBao rejected the charm's credentials. "
                "Removing stored credentials to prevent lockout."
            )
            self.juju_facade.remove_secret(label=OPENBAO_CHARM_APPROLE_SECRET_LABEL)
            return None
        if not openbao.is_active_or_standby():
            return None
        return openbao

    def _sync_openbao_autounseal(self, openbao_client: OpenBaoClient) -> None:
        """Go through all the openbao-autounseal relations and send necessary credentials.

        This looks for any outstanding requests for auto-unseal that may have
        been missed. If there are any, it generates the credentials and sets
        them in the relation databag.
        """
        if not self.unit.is_leader():
            logger.debug("Only leader unit can handle a openbao-autounseal request")
            return
        autounseal_provider_manager = AutounsealProviderManager(
            charm=self,
            client=openbao_client,
            provides=self.openbao_autounseal_provides,
            ca_cert=self.tls.pull_tls_file_from_workload(File.CA),
            mount_path=AUTOUNSEAL_MOUNT_PATH,
        )
        outstanding_relations = autounseal_provider_manager.get_outstanding_requests()
        if outstanding_relations:
            openbao_client.enable_secrets_engine(
                SecretsBackend.TRANSIT, autounseal_provider_manager.mount_path
            )
        for relation in outstanding_relations:
            relation_address = self._get_relation_api_address(relation)
            if not relation_address:
                logger.warning("Relation address not found for relation %s", relation.id)
                continue
            autounseal_provider_manager.create_credentials(relation, relation_address)
        autounseal_provider_manager.clean_up_credentials()

    def generate_openbao_scrape_configs(self) -> List[Dict] | None:
        """Generate the scrape configs for the COS agent.

        Returns:
            The scrape configs for the COS agent or an empty list.
        """
        if not self.juju_facade.relation_exists(PEER_RELATION_NAME):
            return []
        return [
            {
                "scheme": "https",
                "tls_config": {
                    "insecure_skip_verify": False,
                    "ca": self.tls.pull_tls_file_from_workload(File.CA),
                },
                "metrics_path": "/v1/sys/metrics",
                "static_configs": [{"targets": [f"{self._bind_address}:{OPENBAO_PORT}"]}],
            }
        ]

    @contextmanager
    def temp_maintenance_status(self, message: str):
        """Context manager to set the charm status temporarily.

        Useful around long-running operations to indicate that the charm is
        busy.
        """
        previous_status = self.unit.status
        self.unit.status = MaintenanceStatus(message)
        yield
        self.unit.status = previous_status

    def _on_authorize_charm_action(self, event: ActionEvent):
        """Handle the authorize-charm action.

        Grants the charm access to interact with OpenBao
        """
        if not self.unit.is_leader():
            event.fail("This action can only be run by the leader unit")
            return

        secret_id = event.params.get("secret-id", "")
        try:
            if not (
                token := self.juju_facade.get_latest_secret_content(id=secret_id).get("token", "")
            ):
                logger.warning("Token not found in the secret when authorizing charm.")
                event.fail("Token not found in the secret. Please provide a valid token secret.")
                return
        except (NoSuchSecretError, SecretRemovedError):
            logger.warning(
                "Secret id provided could not be found by the charm when authorizing charm."
            )
            event.fail(
                "The secret id provided could not be found by the charm. Please grant the token secret to the charm."
            )
            return

        logger.info("Authorizing the charm to interact with OpenBao")
        if not self._api_address:
            logger.warning("API address is not available when authorizing charm")
            event.fail("API address is not available.")
            return
        if not self.tls.tls_file_available_in_charm(File.CA):
            event.fail("CA certificate is not available in the charm. Something is wrong.")
            return
        openbao = self._get_openbao_client()
        if not openbao:
            logger.warning("Failed to initialize the OpenBao client when authorizing charm")
            event.fail("Failed to initialize the OpenBao client")
            return
        try:
            if not openbao.authenticate(Token(token)):
                logger.warning("Failed to authenticate with OpenBao when authorizing charm")
                event.fail("Failed to authenticate with OpenBao")
                return
        except OpenBaoAuthenticationError:
            logger.warning("Failed to authenticate with OpenBao when authorizing charm")
            event.fail("Failed to authenticate with OpenBao")
            return
        try:
            openbao.enable_approle_auth_method()
            openbao.create_or_update_policy_from_file(
                name=OPENBAO_CHARM_POLICY_NAME, path=OPENBAO_CHARM_POLICY_PATH
            )
            role_id = openbao.create_or_update_approle(
                name="charm",
                policies=[OPENBAO_CHARM_POLICY_NAME, OPENBAO_DEFAULT_POLICY_NAME],
                token_ttl="1h",
                token_max_ttl="1h",
            )
            openbao_secret_id = openbao.generate_role_secret_id(name="charm")
            self.juju_facade.set_app_secret_content(
                content={"role-id": role_id, "secret-id": openbao_secret_id},
                label=OPENBAO_CHARM_APPROLE_SECRET_LABEL,
                description="The authentication details for the charm's access to openbao.",
            )
            event.set_results(
                {"result": "Charm authorized successfully. You may now remove the secret."}
            )
        except OpenBaoClientError as e:
            logger.exception("OpenBao returned an error while authorizing the charm")
            event.fail(f"OpenBao returned an error while authorizing the charm: {str(e)}")
            return

    def _on_bootstrap_raft_action(self, event: ActionEvent):
        """Bootstraps the raft cluster when a single node is present.

        This is useful when OpenBao has lost quorum. The application must first
        be reduced to a single unit.
        """
        if not self._api_address:
            event.fail(message="Network bind address is not available")
            return

        try:
            manager = RaftManager(self, self.machine, OPENBAO_SNAP_NAME, OPENBAO_STORAGE_PATH)
            manager.bootstrap(self._node_id, self._api_address)
        except ManagerError as e:
            logger.error("Failed to bootstrap raft: %s", e)
            event.fail(message=f"Failed to bootstrap raft: {e}")
            return
        event.set_results({"result": "Raft cluster bootstrapped successfully."})

    def _get_openbao_client(self) -> OpenBaoClient | None:
        if not self._api_address:
            return None
        if not self.tls.tls_file_available_in_charm(File.CA):
            return None
        return OpenBaoClient(
            url=self._api_address,
            ca_cert_path=self.tls.get_tls_file_path_in_charm(File.CA),
        )

    def _on_collect_status(self, event: CollectStatusEvent):  # noqa: C901
        """Handle the collect status event."""
        if self.juju_facade.relation_exists(TLS_CERTIFICATE_ACCESS_RELATION_NAME):
            if not sans_dns_config_is_valid(self.juju_facade.get_string_config("access_sans_dns")):
                event.add_status(
                    BlockedStatus(
                        "Config value for access_sans_dns is not valid, it must be a comma separated list"
                    )
                )
                return
            if not sans_ip_config_is_valid(self.juju_facade.get_string_config("access_sans_ip")):
                event.add_status(
                    BlockedStatus(
                        "Config value for access_sans_ip is not valid, it must be a comma separated list of IP addresses"
                    )
                )
                return
        pki_config_needed = self.juju_facade.relation_exists(
            TLS_CERTIFICATES_PKI_RELATION_NAME
        ) or self.juju_facade.relation_exists(PKI_RELATION_NAME)
        if pki_config_needed:
            if not common_name_config_is_valid(
                self.juju_facade.get_string_config("pki_ca_common_name")
            ):
                event.add_status(
                    BlockedStatus(
                        "pki_ca_common_name is not set in the charm config, cannot configure PKI secrets engine"
                    )
                )
                return
            if not allowed_domains_config_is_valid(
                self.juju_facade.get_string_config("pki_allowed_domains")
            ):
                event.add_status(
                    BlockedStatus(
                        "Config value for pki_allowed_domains is not valid, it must be a comma separated list"
                    )
                )
                return
            if not sans_dns_config_is_valid(self.juju_facade.get_string_config("pki_ca_sans_dns")):
                event.add_status(
                    BlockedStatus(
                        "Config value for pki_ca_sans_dns is not valid, it must be a comma separated list"
                    )
                )
                return
        if self.juju_facade.relation_exists(TLS_CERTIFICATES_ACME_RELATION_NAME):
            if not common_name_config_is_valid(
                self.juju_facade.get_string_config("acme_ca_common_name")
            ):
                event.add_status(
                    BlockedStatus(
                        "acme_ca_common_name is not set in the charm config, cannot configure ACME server"
                    )
                )
                return
            if not allowed_domains_config_is_valid(
                self.juju_facade.get_string_config("acme_allowed_domains")
            ):
                event.add_status(
                    BlockedStatus(
                        "Config value for acme_allowed_domains is not valid, it must be a comma separated list"
                    )
                )
                return
            if not sans_dns_config_is_valid(
                self.juju_facade.get_string_config("acme_ca_sans_dns")
            ):
                event.add_status(
                    BlockedStatus(
                        "Config value for acme_ca_sans_dns is not valid, it must be a comma separated list"
                    )
                )
                return

        if not self._log_level_is_valid(self._get_log_level()):
            event.add_status(BlockedStatus("log_level config is not valid"))
            return
        if not self.juju_facade.relation_exists(PEER_RELATION_NAME):
            event.add_status(WaitingStatus("Waiting for peer relation"))
            return
        if not self._bind_address:
            event.add_status(WaitingStatus("Waiting for bind address"))
            return
        if not self.unit.is_leader() and len(self._other_peer_node_api_addresses()) == 0:
            event.add_status(WaitingStatus("Waiting for other units to provide their addresses"))
            return
        if not self.tls.tls_file_pushed_to_workload(File.CA):
            event.add_status(WaitingStatus("Waiting for CA certificate in workload"))
            return
        if not self._api_address:
            event.add_status(WaitingStatus("No address received from Juju yet"))
            return
        if not self.tls.tls_file_available_in_charm(File.CA):
            event.add_status(WaitingStatus("Certificate is unavailable in the charm"))
            return
        if not self._is_openbao_service_started():
            event.add_status(WaitingStatus("Waiting for OpenBao service to start"))
            return
        openbao = self._get_openbao_client()
        if not openbao:
            event.add_status(ErrorStatus("Failed to initialize the OpenBao client"))
            return
        if not openbao.is_api_available():
            event.add_status(WaitingStatus("OpenBao API is not yet available"))
            return
        if not openbao.is_initialized():
            if openbao.is_seal_type_transit():
                event.add_status(BlockedStatus("Please initialize OpenBao"))
                return

            event.add_status(
                BlockedStatus(
                    "Please initialize OpenBao or integrate with an auto-unseal provider"
                )
            )
            return
        try:
            if openbao.is_sealed():
                if openbao.needs_migration():
                    event.add_status(BlockedStatus("Please migrate OpenBao"))
                    return
                if openbao.is_seal_type_transit():
                    event.add_status(WaitingStatus("Waiting for transit auto-unseal"))
                    return
                event.add_status(BlockedStatus("Please unseal OpenBao"))
                return
        except OpenBaoClientError:
            event.add_status(
                MaintenanceStatus("Seal check failed, waiting for OpenBao to recover")
            )
            return
        if not (approle := self._get_openbao_approle_secret()):
            event.add_status(
                BlockedStatus("Please authorize charm (see `authorize-charm` action)")
            )
            return
        try:
            if not openbao.authenticate(approle):
                event.add_status(WaitingStatus("Waiting for OpenBao to become available"))
                return
        except OpenBaoAuthenticationError:
            event.add_status(
                BlockedStatus("Please authorize charm (see `authorize-charm` action)")
            )
            return
        if not openbao.is_active_or_standby():
            event.add_status(WaitingStatus("Waiting for openbao to finish raft leader election"))
            return
        event.add_status(ActiveStatus())

    def _configure(self, _):  # noqa: C901
        """Handle OpenBao installation.

        This includes:
          - Installing the OpenBao snap
          - Generating the OpenBao config file
        """
        self._create_backend_directory()
        self._create_certs_directory()
        self._generate_logrotate_conf()
        try:
            self._install_openbao_snap()
        except (snap.SnapError, ModelError, NameError) as e:
            logger.error("Failed to install OpenBao snap: %s", e)
            return
        if not self.juju_facade.relation_exists(PEER_RELATION_NAME):
            return
        if not self._bind_address:
            return
        if not self.juju_facade.is_leader:
            if len(self._other_peer_node_api_addresses()) == 0:
                return
            if not self.tls.ca_certificate_is_saved():
                return
        if not self._log_level_is_valid(self._get_log_level()):
            return
        config_changed = self._generate_openbao_config_file()
        env_changed = self._sync_openbao_environment()

        # A raft node completes its join to the cluster using the retry_join
        # targets read at startup, so restart an uninitialized node when the
        # config changes to pick up the current active node. Restarting is
        # harmless at that point since the node holds no data.
        restart_needed = env_changed or (config_changed and self._openbao_is_uninitialized())
        if restart_needed and self._openbao_service_is_running():
            self._restart_openbao_service()
        else:
            try:
                self._start_openbao_service()
            except snap.SnapError as e:
                logger.error("Failed to start OpenBao service: %s", e)
                return
        self._set_peer_relation_node_api_address()

        unauthenticated_openbao = self._get_openbao_client()
        if unauthenticated_openbao:
            try:
                if (
                    unauthenticated_openbao.is_api_available()
                    and unauthenticated_openbao.is_initialized()
                    and unauthenticated_openbao.is_sealed()
                    and unauthenticated_openbao.is_seal_type_transit()
                ):
                    logger.info(
                        "OpenBao is sealed with transit seal type, restarting to trigger auto-unseal"
                    )
                    self._restart_openbao_service()
                    return
            except OpenBaoClientError:
                pass

        openbao = self._get_authenticated_openbao_client()
        if not openbao:
            return
        self._configure_pki_secrets_engine(openbao)
        self._configure_acme_server(openbao)
        self._sync_openbao_autounseal(openbao)
        self._sync_openbao_kv(openbao)
        self._sync_openbao_pki(openbao)

        if not self._api_address or not self.tls.tls_file_available_in_charm(File.CA):
            return

        if openbao.is_active() and not openbao.is_raft_cluster_healthy():
            logger.warning("Raft cluster is not healthy: %s", openbao.get_raft_cluster_state())

    def _on_remove(self, event: RemoveEvent):
        """Handle remove charm event.

        Removes the openbao service and the raft data and removes the node from the raft cluster.
        """
        self._remove_node_from_raft_cluster()
        if self._openbao_service_is_running():
            self.machine.stop(OPENBAO_SNAP_NAME)
        self._delete_openbao_data()

    def _on_create_backup_action(self, event: ActionEvent) -> None:
        """Handle the create-backup action.

        Creates a snapshot and stores it on S3 storage.
        Outputs the ID of the backup to the user.

        Args:
            event: ActionEvent
        """
        skip_verify: bool = event.params.get("skip-verify", False)

        openbao_client = self._get_authenticated_openbao_client()
        if not openbao_client:
            event.fail(message="Failed to initialize OpenBao client.")
            return
        try:
            manager = BackupManager(self, self.s3_requirer, S3_RELATION_NAME)
            backup_key = manager.create_backup(openbao_client, skip_verify=skip_verify)
        except ManagerError as e:
            logger.error("Failed to create backup: %s", e)
            event.fail(message=f"Failed to create backup: {e}")
            return
        event.set_results({"backup-id": backup_key})

    def _on_list_backups_action(self, event: ActionEvent) -> None:
        """Handle the list-backups action.

        Lists all backups stored in S3 bucket.
        """
        skip_verify: bool = event.params.get("skip-verify", False)

        try:
            manager = BackupManager(self, self.s3_requirer, S3_RELATION_NAME)
            backup_ids = manager.list_backups(skip_verify=skip_verify)
        except ManagerError as e:
            logger.error("Failed to list backups: %s", e)
            event.fail(message=f"Failed to list backups: {e}")
            return

        event.set_results({"backup-ids": json.dumps(backup_ids)})

    def _on_restore_backup_action(self, event: ActionEvent) -> None:
        """Handle the restore-backup action.

        Restores the snapshot with the provided ID.
        """
        openbao_client = self._get_active_openbao_client()
        if not openbao_client:
            event.fail(message="Failed to initialize an active OpenBao client.")
            return
        key = event.params.get("backup-id")
        # This should be enforced by Juju/charmcraft.yaml, but we assert here
        # to make the typechecker happy
        assert isinstance(key, str)
        skip_verify: bool = event.params.get("skip-verify", False)

        try:
            manager = BackupManager(self, self.s3_requirer, S3_RELATION_NAME)
            manager.restore_backup(openbao_client, key, skip_verify=skip_verify)
        except ManagerError as e:
            logger.error("Failed to restore backup: %s", e)
            event.fail(message=f"Failed to restore backup: {e}")
            return

        event.set_results({"restored": event.params.get("backup-id")})

    def _openbao_service_is_running(self) -> bool:
        """Check if the OpenBao service is running."""
        service = self.machine.get_service(process=OPENBAO_PROCESS_NAME)
        return False if not service else service.is_running()

    def _delete_openbao_data(self) -> None:
        """Delete OpenBao's data."""
        try:
            self.machine.remove_path(path=f"{OPENBAO_STORAGE_PATH}/vault.db")
            logger.info("Removed OpenBao's main database")
        except ValueError:
            logger.info("No OpenBao database to remove")
        try:
            self.machine.remove_path(path=f"{OPENBAO_STORAGE_PATH}/raft/raft.db")
            logger.info("Removed OpenBao's Raft database")
        except ValueError:
            logger.info("No OpenBao raft database to remove")

    def _remove_node_from_raft_cluster(self):
        """Remove the node from the raft cluster."""
        if not (approle := self._get_openbao_approle_secret()):
            logger.error("Failed to authenticate to OpenBao")
            return
        api_address = self._api_address
        if not api_address:
            logger.error("Can't remove node from cluster - OpenBao API address is not available")
            return
        openbao = OpenBaoClient(url=api_address, ca_cert_path=None)
        if not openbao.is_api_available():
            logger.error("Can't remove node from cluster - OpenBao API is not available")
            return
        if not openbao.is_initialized():
            logger.error("Can't remove node from cluster - OpenBao is not initialized")
            return
        try:
            if openbao.is_sealed():
                logger.error("Can't remove node from cluster - OpenBao is sealed")
                return
        except OpenBaoClientError as e:
            logger.error("Can't remove node from cluster - OpenBao status check failed: %s", e)
            return
        try:
            openbao.authenticate(approle)
        except OpenBaoAuthenticationError:
            logger.warning("OpenBao rejected the charm's credentials during raft node removal")
            return
        if openbao.is_node_in_raft_peers(id=self._node_id) and openbao.get_num_raft_peers() > 1:
            openbao.remove_raft_node(id=self._node_id)

    def _check_s3_pre_requisites(self) -> str | None:
        """Check if the S3 pre-requisites are met."""
        if not self.unit.is_leader():
            return "Only leader unit can perform backup operations"
        if not self.juju_facade.relation_exists(S3_RELATION_NAME):
            return "S3 relation not created"
        if missing_parameters := self._get_missing_s3_parameters():
            return "S3 parameters missing ({})".format(", ".join(missing_parameters))
        return None

    def _get_backup_key(self) -> str:
        """Return the backup key.

        Returns:
            str: The backup key
        """
        timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        return f"{BACKUP_KEY_PREFIX}-{self.model.name}-{timestamp}"

    def _get_s3_parameters(self) -> Dict[str, str]:
        """Retrieve S3 parameters from the S3 integrator relation.

        Removes leading and trailing whitespaces from the parameters.

        Returns:
            Dict[str, str]: Dictionary of the S3 parameters.
        """
        s3_parameters = self.s3_requirer.get_s3_connection_info()
        for key, value in s3_parameters.items():
            if isinstance(value, str):
                s3_parameters[key] = value.strip()
        return s3_parameters

    def _get_missing_s3_parameters(self) -> List[str]:
        """Return the list of missing S3 parameters.

        Returns:
            List[str]: List of missing required S3 parameters.
        """
        s3_parameters = self.s3_requirer.get_s3_connection_info()
        return [param for param in REQUIRED_S3_PARAMETERS if param not in s3_parameters]

    def _get_relation_api_address(self, relation: Relation) -> str:
        """Get the API address for the given relation."""
        ingress_address = self.juju_facade.get_ingress_address(relation=relation)
        return f"https://{ingress_address}:{OPENBAO_PORT}"

    def _sync_openbao_kv(self, openbao: OpenBaoClient) -> None:
        """Goes through all the openbao-kv relations and sends necessary KV information."""
        if not self.juju_facade.is_leader:
            logger.debug("Only leader unit can handle a openbao-kv request")
            return
        ca_certificate = self.tls.pull_tls_file_from_workload(File.CA)
        if not ca_certificate:
            logger.debug("OpenBao CA certificate not available")
            return
        manager = KVManager(self, openbao, self.openbao_kv, ca_certificate)

        kv_requests = self.openbao_kv.get_kv_requests()
        for kv_request in kv_requests:
            if not (openbao_url := self._get_relation_api_address(kv_request.relation)):
                logger.debug("Failed to get OpenBao URL for relation %s", kv_request.relation.id)
                continue
            manager.generate_credentials_for_requirer(
                relation=kv_request.relation,
                app_name=kv_request.app_name,
                unit_name=kv_request.unit_name,
                mount_suffix=kv_request.mount_suffix,
                egress_subnets=kv_request.egress_subnets,
                nonce=kv_request.nonce,
                openbao_url=openbao_url,
            )

    def _sync_openbao_pki(self, openbao_client: OpenBaoClient) -> None:
        """Goes through all the openbao-pki relations and sends necessary TLS certificate."""
        if not common_name_config_is_valid(
            self.juju_facade.get_string_config("pki_ca_common_name")
        ):
            return
        if not allowed_domains_config_is_valid(
            self.juju_facade.get_string_config("pki_ca_allowed_domains")
        ):
            return
        if not sans_dns_config_is_valid(self.juju_facade.get_string_config("pki_ca_sans_dns")):
            return
        certificate_request = self._get_pki_certificate_request()
        if not certificate_request:
            return
        # Pass tls_certificates_pki only if relation exists; PKIManager derives mode from this
        tls_certificates_pki = (
            self.tls_certificates_pki
            if self.juju_facade.relation_exists(TLS_CERTIFICATES_PKI_RELATION_NAME)
            else None
        )
        manager = PKIManager(
            charm=self,
            openbao_client=openbao_client,
            certificate_request_attributes=certificate_request,
            mount_point=OPENBAO_PKI_MOUNT,
            role_name=OPENBAO_PKI_ROLE,
            openbao_pki=self.openbao_pki,
            tls_certificates_pki=tls_certificates_pki,
            allowed_domains=self.juju_facade.get_string_config("pki_allowed_domains"),
            allow_bare_domains=self.juju_facade.get_bool_config("pki_allow_bare_domains"),
            allow_subdomains=self.juju_facade.get_bool_config("pki_allow_subdomains"),
            allow_wildcard_certificates=self.juju_facade.get_bool_config(
                "pki_allow_wildcard_certificates"
            ),
            allow_any_name=self.juju_facade.get_bool_config("pki_allow_any_name"),
            allow_ip_sans=self.juju_facade.get_bool_config("pki_allow_ip_sans"),
            organization=self.juju_facade.get_string_config("pki_organization"),
            organizational_unit=self.juju_facade.get_string_config("pki_organizational_unit"),
            country=self.juju_facade.get_string_config("pki_country"),
            province=self.juju_facade.get_string_config("pki_province"),
            locality=self.juju_facade.get_string_config("pki_locality"),
            self_signed_ca_validity_hours=self.juju_facade.get_int_config(
                "pki_self_signed_ca_validity"
            )
            or 87600,
        )
        manager.sync()

    def _configure_pki_secrets_engine(self, openbao: OpenBaoClient) -> None:  # noqa: C901
        """Configure the PKI secrets engine."""
        if not common_name_config_is_valid(
            self.juju_facade.get_string_config("pki_ca_common_name")
        ):
            logger.warning(
                "pki_ca_common_name is not set in the charm config, not configuring PKI secrets engine"
            )
            return
        if not allowed_domains_config_is_valid(
            self.juju_facade.get_string_config("pki_allowed_domains")
        ):
            logger.warning(
                "pki_ca_allowed_domains has invalid value, must be a comma separated list, skipping PKI secrets engine configuration"
            )
            return
        if not sans_dns_config_is_valid(self.juju_facade.get_string_config("pki_ca_sans_dns")):
            logger.warning(
                "pki_ca_sans_dns has invalid value, must be a comma separated list, skipping PKI secrets engine configuration"
            )
            return
        certificate_request = self._get_pki_certificate_request()
        if not certificate_request:
            return
        # Pass tls_certificates_pki only if relation exists; PKIManager derives mode from this
        tls_certificates_pki = (
            self.tls_certificates_pki
            if self.juju_facade.relation_exists(TLS_CERTIFICATES_PKI_RELATION_NAME)
            else None
        )
        manager = PKIManager(
            charm=self,
            openbao_client=openbao,
            certificate_request_attributes=certificate_request,
            mount_point=OPENBAO_PKI_MOUNT,
            role_name=OPENBAO_PKI_ROLE,
            tls_certificates_pki=tls_certificates_pki,
            openbao_pki=self.openbao_pki,
            allowed_domains=self.juju_facade.get_string_config("pki_allowed_domains"),
            allow_bare_domains=self.juju_facade.get_bool_config("pki_allow_bare_domains"),
            allow_subdomains=self.juju_facade.get_bool_config("pki_allow_subdomains"),
            allow_wildcard_certificates=self.juju_facade.get_bool_config(
                "pki_allow_wildcard_certificates"
            ),
            allow_any_name=self.juju_facade.get_bool_config("pki_allow_any_name"),
            allow_ip_sans=self.juju_facade.get_bool_config("pki_allow_ip_sans"),
            organization=self.juju_facade.get_string_config("pki_organization"),
            organizational_unit=self.juju_facade.get_string_config("pki_organizational_unit"),
            country=self.juju_facade.get_string_config("pki_country"),
            province=self.juju_facade.get_string_config("pki_province"),
            locality=self.juju_facade.get_string_config("pki_locality"),
            self_signed_ca_validity_hours=self.juju_facade.get_int_config(
                "pki_self_signed_ca_validity"
            )
            or 87600,
        )
        manager.configure()

    def _get_pki_certificate_request(self) -> CertificateRequestAttributes | None:
        common_name = self.juju_facade.get_string_config("pki_ca_common_name")
        if not common_name:
            logger.warning("pki_ca_common_name is not set in the charm config")
            return None
        sans_dns = self.juju_facade.get_string_config("pki_ca_sans_dns")
        if not sans_dns_config_is_valid(sans_dns):
            logger.warning("pki_ca_sans_dns is not valid")
            return None
        if sans_dns:
            sans_dns = [name.strip() for name in sans_dns.split(",")]
        return CertificateRequestAttributes(
            common_name=common_name,
            sans_dns=frozenset(sans_dns) if sans_dns else frozenset(),
            country_name=self.juju_facade.get_string_config("pki_ca_country_name")
            if self.juju_facade.get_string_config("pki_ca_country_name")
            else None,
            state_or_province_name=self.juju_facade.get_string_config(
                "pki_ca_state_or_province_name"
            )
            if self.juju_facade.get_string_config("pki_ca_state_or_province_name")
            else None,
            locality_name=self.juju_facade.get_string_config("pki_ca_locality_name")
            if self.juju_facade.get_string_config("pki_ca_locality_name")
            else None,
            organization=self.juju_facade.get_string_config("pki_ca_organization")
            if self.juju_facade.get_string_config("pki_ca_organization")
            else None,
            organizational_unit=self.juju_facade.get_string_config("pki_ca_organizational_unit")
            if self.juju_facade.get_string_config("pki_ca_organizational_unit")
            else None,
            email_address=self.juju_facade.get_string_config("pki_ca_email_address")
            if self.juju_facade.get_string_config("pki_ca_email_address")
            else None,
            is_ca=True,
        )

    def _configure_acme_server(self, openbao: OpenBaoClient) -> None:
        if not common_name_config_is_valid(
            self.juju_facade.get_string_config("acme_ca_common_name")
        ):
            logger.warning(
                "acme_ca_common_name has invalid value, skipping ACME server configuration"
            )
            return
        if not allowed_domains_config_is_valid(
            self.juju_facade.get_string_config("acme_allowed_domains")
        ):
            logger.warning(
                "acme_allowed_domains has invalid value, must be a comma separated list, skipping PKI secrets engine configuration"
            )
            return
        if not sans_dns_config_is_valid(self.juju_facade.get_string_config("acme_ca_sans_dns")):
            logger.warning(
                "acme_ca_sans_dns has invalid value, must be a comma separated list, skipping PKI secrets engine configuration"
            )
            return
        certificate_request = self._get_acme_certificate_request()
        if not certificate_request:
            return
        manager = ACMEManager(
            charm=self,
            openbao_client=openbao,
            mount_point=ACME_MOUNT,
            tls_certificates_acme=self.tls_certificates_acme,
            certificate_request_attributes=certificate_request,
            role_name=ACME_ROLE_NAME,
            openbao_address=f"https://{self._ingress_address}:{OPENBAO_PORT}",
            allowed_domains=self.juju_facade.get_string_config("acme_allowed_domains"),
            allow_bare_domains=self.juju_facade.get_bool_config("acme_allow_bare_domains"),
            allow_subdomains=self.juju_facade.get_bool_config("acme_allow_subdomains"),
            allow_wildcard_certificates=self.juju_facade.get_bool_config(
                "acme_allow_wildcard_certificates"
            ),
            allow_any_name=self.juju_facade.get_bool_config("acme_allow_any_name"),
            allow_ip_sans=self.juju_facade.get_bool_config("acme_allow_ip_sans"),
            organization=self.juju_facade.get_string_config("acme_organization"),
            organizational_unit=self.juju_facade.get_string_config("acme_organizational_unit"),
            country=self.juju_facade.get_string_config("acme_country"),
            province=self.juju_facade.get_string_config("acme_province"),
            locality=self.juju_facade.get_string_config("acme_locality"),
        )
        manager.configure()

    def _get_acme_certificate_request(self) -> CertificateRequestAttributes | None:
        common_name = self.juju_facade.get_string_config("acme_ca_common_name")
        if not common_name:
            logger.warning("acme_ca_common_name is not set in the charm config")
            return None
        sans_dns = self.juju_facade.get_string_config("acme_ca_sans_dns")
        if not sans_dns_config_is_valid(sans_dns):
            logger.warning("acme_ca_sans_dns is not valid")
            return None
        if sans_dns:
            sans_dns = [name.strip() for name in sans_dns.split(",")]
        return CertificateRequestAttributes(
            common_name=common_name,
            sans_dns=frozenset(sans_dns) if sans_dns else frozenset(),
            country_name=self.juju_facade.get_string_config("acme_ca_country_name")
            if self.juju_facade.get_string_config("acme_ca_country_name")
            else None,
            state_or_province_name=self.juju_facade.get_string_config(
                "acme_ca_state_or_province_name"
            )
            if self.juju_facade.get_string_config("acme_ca_state_or_province_name")
            else None,
            locality_name=self.juju_facade.get_string_config("acme_ca_locality_name")
            if self.juju_facade.get_string_config("acme_ca_locality_name")
            else None,
            organization=self.juju_facade.get_string_config("acme_ca_organization")
            if self.juju_facade.get_string_config("acme_ca_organization")
            else None,
            organizational_unit=self.juju_facade.get_string_config("acme_ca_organizational_unit")
            if self.juju_facade.get_string_config("acme_ca_organizational_unit")
            else None,
            email_address=self.juju_facade.get_string_config("acme_ca_email_address")
            if self.juju_facade.get_string_config("acme_ca_email_address")
            else None,
            is_ca=True,
        )

    def _get_default_lease_ttl(self) -> str:
        """Return the default lease ttl config."""
        default_lease_ttl = self.config.get("default_lease_ttl")
        if not default_lease_ttl or not isinstance(default_lease_ttl, str):
            raise ValueError("Invalid config default_lease_ttl")
        return default_lease_ttl

    def _get_max_lease_ttl(self) -> str:
        """Return the max lease ttl config."""
        max_lease_ttl = self.config.get("max_lease_ttl")
        if not max_lease_ttl or not isinstance(max_lease_ttl, str):
            raise ValueError("Invalid config max_lease_ttl")
        return max_lease_ttl

    def _get_log_level(self) -> str:
        """Return the log level config."""
        log_level = self.config.get("log_level")
        if not log_level or not isinstance(log_level, str):
            raise ValueError("Invalid config log_level")
        return log_level

    def _log_level_is_valid(self, log_level: str) -> bool:
        return log_level in ["trace", "debug", "info", "warn", "error"]

    def _get_logrotate_frequency(self) -> str:
        """Return the logrotate frequency config."""
        logrotate_frequency = str(self.config.get("logrotate_frequency"))
        if not self._logrotate_frequency_is_valid(str(logrotate_frequency)):
            raise ValueError(f"Invalid logrotatde frequency: {logrotate_frequency}")
        return logrotate_frequency

    def _logrotate_frequency_is_valid(self, logrotate_frequency: str) -> bool:
        return logrotate_frequency in ["daily", "weekly", "monthly"]

    def _generate_logrotate_conf(self) -> None:
        """Write the logrotate configuration file."""
        logrotate_conf_template = """\
        /var/log/syslog
        {{
            rotate {rotate_count}
            {frequency}
            maxsize {maxsize}
            missingok
            notifempty
            compress
            delaycompress
            postrotate
                /usr/lib/rsyslog/rsyslog-rotate
            endscript
        }}
        """
        logrotate_frequency = self._get_logrotate_frequency()
        logrotate_conf = logrotate_conf_template.format(
            rotate_count=LOGROTATE_DEFAULT_COUNT,
            frequency=logrotate_frequency,
            maxsize=LOGROTATE_DEFAULT_MAXSIZE,
        )
        logger.debug(logrotate_conf)
        _ = LOGROTATE_PATH.write_text(logrotate_conf)

    def _get_openbao_approle_secret(self) -> AppRole | None:
        """Get the approle details from the secret.

        Returns:
            AppRole: An AppRole object with role_id and secret_id set from the
                     values stored in the Juju secret, or None if the secret is
                     not found or either of the values are not set.
        """
        try:
            role_id, secret_id = self.juju_facade.get_secret_content_values(
                "role-id", "secret-id", label=OPENBAO_CHARM_APPROLE_SECRET_LABEL, refresh=True
            )
        except NoSuchSecretError:
            logger.warning("Approle secret not yet created")
            return None
        return AppRole(role_id, secret_id) if role_id and secret_id else None

    @property
    def _juju_proxy_environment(self) -> dict[str, str]:
        """Extract Juju proxy model environment variables.

        Returns:
            Dictionary of proxy environment variables (HTTP_PROXY, HTTPS_PROXY, NO_PROXY)
        """
        env = {}
        if http_proxy := get_env_var("JUJU_CHARM_HTTP_PROXY"):
            env["HTTP_PROXY"] = http_proxy
        if https_proxy := get_env_var("JUJU_CHARM_HTTPS_PROXY"):
            env["HTTPS_PROXY"] = https_proxy
        if no_proxy := get_env_var("JUJU_CHARM_NO_PROXY"):
            env["NO_PROXY"] = no_proxy
        return env

    def _install_openbao_snap(self) -> None:
        """Install the OpenBao snap from the charm's snap resource.

        The snap is attached to the charm as a file resource rather than
        fetched from the snap store.
        """
        try:
            snap_cache = snap.SnapCache()
            openbao_snap = snap_cache[OPENBAO_SNAP_NAME]
            if openbao_snap.state in [
                snap.SnapState.Latest,
                snap.SnapState.Present,
            ]:
                logger.debug("OpenBao snap is already installed")
                return
            snap_path = self.model.resources.fetch(OPENBAO_SNAP_RESOURCE_NAME)
            with self.temp_maintenance_status("Installing OpenBao"):
                snap.install_local(str(snap_path), dangerous=True)
            logger.info("OpenBao snap installed")
            if self._openbao_service_is_running():
                self.machine.stop(OPENBAO_SNAP_NAME)
                logger.debug("Previously running OpenBao service stopped")
        except snap.SnapError as e:
            logger.error("An exception occurred when installing OpenBao. Reason: %s", str(e))
            raise e

    def _create_backend_directory(self) -> None:
        self.machine.make_dir(path=OPENBAO_STORAGE_PATH)

    def _create_certs_directory(self) -> None:
        self.machine.make_dir(path=MACHINE_TLS_FILE_DIRECTORY_PATH)

    def _start_openbao_service(self) -> None:
        """Start the OpenBao service."""
        snap_cache = snap.SnapCache()
        openbao_snap = snap_cache[OPENBAO_SNAP_NAME]
        openbao_snap.start(services=[OPENBAO_SNAP_SERVICE_NAME])
        logger.debug("OpenBao service started")

    def _sync_openbao_environment(self) -> bool:
        """Add or remove the systemd drop-in file for the OpenBao service.

        This file is used to set the BAO_TOKEN environment variable for the
        external OpenBao service when using auto-unseal, and to set proxy environment
        variables (HTTP_PROXY, HTTPS_PROXY, NO_PROXY) when available.

        If no token and no proxy environment variables are available, the systemd drop-in
        is removed and openbao.env is cleared.

        Returns:
            True if environment files were updated, False otherwise
        """
        token = self._get_openbao_autounseal_token()
        proxy_env = self._juju_proxy_environment

        if not token and not proxy_env:
            logger.debug("No auto-unseal token or proxy environment variables available")
            with suppress(ValueError):
                self.machine.remove_path(SYSTEMD_DROP_IN_FILE_PATH)
                self.machine.remove_path(OPENBAO_ENV_PATH)
                logger.info("Removed systemd drop-in file and openbao.env")
            return False

        if token:
            try:
                SystemdCreds.encrypt_if_changed(SYSTEMD_CRED_EXTERNAL_BAO_TOKEN_NAME, token)
            except subprocess.CalledProcessError:
                logger.warning("Failed to encrypt auto-unseal token")

        if self._generate_systemd_drop_in_file(token, proxy_env):
            SystemdCreds.reload_daemon()
            return True
        return False

    def _generate_systemd_drop_in_file(
        self, external_openbao_token: str | None, proxy_env: dict[str, str]
    ) -> bool:
        """Create the systemd drop-in file for the OpenBao service.

        This file is a bit like an overlay, and adds some extra configuration
        to a service. In particular, we use this file to pass the encrypted
        external openbao token to the service (if supported), or otherwise inject
        the token as an environment variable. It also sets proxy environment
        variables (HTTP_PROXY, HTTPS_PROXY, NO_PROXY) if available.

        If the token is passed via a credential, we also update the `openbao.env`
        file to load this credential into the BAO_TOKEN env var.

        Args:
            external_openbao_token: The autounseal token, or None if not using autounseal
            proxy_env: Dictionary of proxy environment variables

        Returns:
            True if the file was created, False otherwise
        """
        jinja2 = Environment(loader=FileSystemLoader(TEMPLATE_PATH))
        self.machine.make_dir(path=SYSTEMD_DROP_IN_DIR)

        credentials_supported = SystemdCreds.is_credentials_supported()

        # If we have a token but credentials are not supported, log a warning
        if external_openbao_token and not credentials_supported:
            logger.warning(
                "This system configuration does not support systemd credentials. Falling back to un-encrypted environment variables."
            )

        dropin_content = jinja2.get_template(TEMPLATE_SYSTEMD_DROP_IN_CREDS).render(
            credential_name=SYSTEMD_CRED_EXTERNAL_BAO_TOKEN_NAME
            if external_openbao_token and credentials_supported
            else None,
            external_openbao_token=external_openbao_token
            if external_openbao_token and not credentials_supported
            else None,
            proxy_env=proxy_env,
        )

        # Check if file exists and content has changed
        if self.machine.exists(path=SYSTEMD_DROP_IN_FILE_PATH):
            existing_content = self.machine.pull(path=SYSTEMD_DROP_IN_FILE_PATH).read()
            if existing_content == dropin_content:
                return False

        # Push the drop-in file
        self.machine.push(path=SYSTEMD_DROP_IN_FILE_PATH, source=dropin_content)

        # If using systemd credentials, also update openbao.env to load the credential
        if external_openbao_token and SystemdCreds.is_credentials_supported():
            openbao_env_content = jinja2.get_template(
                TEMPLATE_OPENBAO_ENV_LOAD_SYSTEMD_CREDS
            ).render(credential_name=SYSTEMD_CRED_EXTERNAL_BAO_TOKEN_NAME)
            self.machine.push(path=OPENBAO_ENV_PATH, source=openbao_env_content)
            logger.info("Updated systemd drop-in file for OpenBao service")

        return True

    def _filter_active_peer_addresses(self, addresses: List[str]) -> List[str]:
        """Return only the active node's address when one can be identified.

        OpenBao, unlike Vault, does not forward raft bootstrap answers received
        by standby nodes to the active node, and it answers the challenge of
        whichever retry_join target responds first. A new node can therefore
        only join the cluster when retry_join points at the active node. When
        no active node is reachable (e.g. before the cluster is initialized),
        fall back to all peer addresses.
        """
        try:
            ca_path = self.tls.get_tls_file_path_in_charm(File.CA)
        except (OpenBaoCertsError, TransientJujuError):
            logger.debug("CA certificate unavailable, not filtering retry_join addresses")
            return addresses
        active_addresses = []
        for address in addresses:
            try:
                if OpenBaoClient(address, ca_cert_path=ca_path, timeout=5).is_active():
                    active_addresses.append(address)
            except Exception:
                logger.debug("Failed to probe %s for active status", address, exc_info=True)
        logger.info("Active peers for retry_join: %s (out of %s)", active_addresses, addresses)
        return active_addresses or addresses

    def _openbao_is_uninitialized(self) -> bool:
        """Return whether the local OpenBao node is not yet part of a cluster.

        A node that has not completed its raft join is reported as not
        initialized. A node that is wedged answering a raft join (its API does
        not respond) is also treated as uninitialized, since restarting it is
        the only way to make it pick up new retry_join targets.
        """
        if not self._api_address:
            return False
        try:
            ca_path = self.tls.get_tls_file_path_in_charm(File.CA)
        except (OpenBaoCertsError, TransientJujuError):
            return False
        client = OpenBaoClient(url=self._api_address, ca_cert_path=ca_path, timeout=5)
        try:
            if not client.is_api_available():
                # The service is running but its API does not respond: either
                # it is still starting up or it is wedged mid raft-join.
                return True
            return not client.is_initialized()
        except OpenBaoClientError:
            return False

    def _generate_openbao_config_file(self) -> bool:
        """Create the OpenBao config file and push it to the Machine.

        Returns:
            True if the config file content changed, False otherwise.
        """
        assert self._cluster_address
        assert self._api_address
        retry_joins = [
            {
                "leader_api_addr": node_api_address,
                "leader_ca_cert_file": f"{MACHINE_TLS_FILE_DIRECTORY_PATH}/{File.CA.name.lower()}.pem",  # noqa: E501
            }
            for node_api_address in self._filter_active_peer_addresses(
                self._other_peer_node_api_addresses()
            )
        ]

        autounseal_configuration_details = self._get_openbao_autounseal_configuration()

        content = render_openbao_config_file(
            config_template_path=TEMPLATE_PATH,
            config_template_name=CONFIG_TEMPLATE_NAME,
            default_lease_ttl=self._get_default_lease_ttl(),
            max_lease_ttl=self._get_max_lease_ttl(),
            cluster_address=self._cluster_address,
            api_address=self._api_address,
            tls_cert_file=f"{MACHINE_TLS_FILE_DIRECTORY_PATH}/{File.CERT.name.lower()}.pem",
            tls_key_file=f"{MACHINE_TLS_FILE_DIRECTORY_PATH}/{File.KEY.name.lower()}.pem",
            tcp_address=f"[::]:{OPENBAO_PORT}",
            raft_storage_path=OPENBAO_STORAGE_PATH,
            node_id=self._node_id,
            retry_joins=retry_joins,
            autounseal_config=autounseal_configuration_details,
            log_level=self._get_log_level(),
        )
        existing_content = ""
        openbao_config_file_path = f"{OPENBAO_CONFIG_PATH}/{OPENBAO_CONFIG_FILE_NAME}"
        if self.machine.exists(path=openbao_config_file_path):
            existing_content_stringio = self.machine.pull(path=openbao_config_file_path)
            existing_content = existing_content_stringio.read()

        if not config_file_content_matches(existing_content=existing_content, new_content=content):
            self.machine.push(
                path=openbao_config_file_path,
                source=content,
            )
            # If the seal type has changed, openbao will be restarted by _sync_openbao_environment()
            # in configure to pick up both config and environment changes together.
            return True
        return False

    def _restart_openbao_service(self) -> None:
        """Restart the OpenBao service."""
        if self._openbao_service_is_running():
            self.machine.restart(OPENBAO_SNAP_NAME)
            logger.debug("OpenBao service restarted")

    def _get_openbao_autounseal_token(self) -> str | None:
        autounseal_relation_details = self.openbao_autounseal_requires.get_details()
        if not autounseal_relation_details:
            return None
        autounseal_requirer_manager = AutounsealRequirerManager(
            self, self.openbao_autounseal_requires
        )
        provider_openbao_token = autounseal_requirer_manager.get_provider_openbao_token(
            autounseal_relation_details, self.tls.get_tls_file_path_in_charm(File.AUTOUNSEAL_CA)
        )
        return provider_openbao_token

    def _get_openbao_autounseal_configuration(self) -> AutounsealConfiguration | None:
        autounseal_relation_details = self.openbao_autounseal_requires.get_details()
        if not autounseal_relation_details:
            return None
        self.tls.push_autounseal_ca_cert(autounseal_relation_details.ca_certificate)
        return AutounsealConfiguration(
            autounseal_relation_details.address,
            autounseal_relation_details.mount_path,
            autounseal_relation_details.key_name,
            self.tls.get_tls_file_path_in_workload(File.AUTOUNSEAL_CA),
        )

    def _set_peer_relation_node_api_address(self) -> None:
        """Set the unit address in the peer relation."""
        assert self._api_address
        self.juju_facade.set_unit_relation_data(
            data={"node_api_address": self._api_address},
            name=PEER_RELATION_NAME,
        )

    def _get_peer_relation_node_api_addresses(self) -> List[str]:
        """Return the list of peer unit addresses."""
        peer_relation_data = self.juju_facade.get_remote_units_relation_data(
            name=PEER_RELATION_NAME,
        )
        return [
            databag["node_api_address"]
            for databag in peer_relation_data
            if "node_api_address" in databag
        ] + ([self._api_address] if self._api_address else [])

    def _other_peer_node_api_addresses(self) -> List[str]:
        """Return the list of other peer unit addresses.

        We exclude our own unit address from the list.
        """
        return [
            node_api_address
            for node_api_address in self._get_peer_relation_node_api_addresses()
            if node_api_address != self._api_address
        ]

    def _is_openbao_service_started(self) -> bool:
        """Check if the OpenBao service is started."""
        snap_cache = snap.SnapCache()
        openbao_snap = snap_cache[OPENBAO_SNAP_NAME]
        openbao_services = openbao_snap.services
        baod_service = openbao_services.get(OPENBAO_SNAP_SERVICE_NAME)
        if not baod_service:
            return False
        if not baod_service["active"]:
            return False
        return True

    @property
    def _bind_address(self) -> str | None:
        """Fetches bind address from peer relation and returns it.

        Returns:
            str: Bind address
        """
        return self.juju_facade.get_bind_address(relation_name=PEER_RELATION_NAME)

    @property
    def _api_address(self) -> str | None:
        """Returns the IP with the https schema and openbao port.

        Example: "https://1.2.3.4:8200"
        """
        if not self._bind_address:
            return None
        return f"https://{self._bind_address}:{OPENBAO_PORT}"

    @property
    def _cluster_address(self) -> str | None:
        """Return the IP with the https schema and openbao port.

        Example: "https://1.2.3.4:8201"
        """
        if not self._bind_address:
            return None
        return f"https://{self._bind_address}:{OPENBAO_CLUSTER_PORT}"

    @property
    def _node_id(self) -> str:
        """Return node id for openbao.

        Example of node id: "openbao-0"
        """
        return f"{self.model.name}-{self.unit.name}"

    @property
    def _ingress_address(self) -> str | None:
        """Fetch the ingress address from peer relation and returns it.

        Returns:
            str: Ingress address
        """
        return self.juju_facade.get_ingress_address(PEER_RELATION_NAME)


if __name__ == "__main__":  # pragma: nocover
    main(OpenBaoOperatorCharm)
