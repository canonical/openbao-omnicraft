#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for OpenBao, deployable on both Kubernetes and machines.

For more information on OpenBao, please visit https://openbao.org/.
"""

import logging
import os
import socket
from typing import Any, Dict, List

from charmlibs.interfaces.tls_certificates import (
    CertificateRequestAttributes,
    Mode,
    TLSCertificatesProvidesV4,
    TLSCertificatesRequiresV4,
)
from ops import (
    ActiveStatus,
    BlockedStatus,
    CharmBase,
    CollectStatusEvent,
    EventBase,
    InstallEvent,
    MaintenanceStatus,
    Relation,
    StatusBase,
    WaitingStatus,
    main,
)

from charms.data_platform_libs.v0.s3 import S3Requirer
from charms.grafana_agent.v0.cos_agent import COSAgentProvider
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.observability_libs.v0.kubernetes_compute_resources_patch import (
    KubernetesComputeResourcesPatch,
    ResourceRequirements,
    adjust_resource_requirements,
)
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.tempo_coordinator_k8s.v0.charm_tracing import trace_charm
from charms.tempo_coordinator_k8s.v0.tracing import TracingEndpointRequirer, charm_tracing_config
from charms.traefik_k8s.v1.ingress_per_unit import IngressPerUnitRequirer
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer
from charms.vault_k8s.v0.vault_kv import VaultKvClientDetachedEvent, VaultKvProvides
from common.exceptions import OpenBaoCertsError
from common.juju_facade import (
    JujuFacade,
    NoSuchSecretError,
    TransientJujuError,
)
from common.openbao_autounseal import OpenBaoAutounsealProvides, OpenBaoAutounsealRequires
from common.openbao_client import (
    AppRole,
    OpenBaoAuthenticationError,
    OpenBaoClient,
    OpenBaoClientError,
    SecretsBackend,
)
from common.openbao_helpers import (
    AutounsealConfiguration,
    allowed_domains_config_is_valid,
    common_name_config_is_valid,
    config_file_content_matches,
    get_env_var,
    render_openbao_config_file,
    sans_dns_config_is_valid,
    sans_ip_config_is_valid,
    seal_type_has_changed,
)
from core.base_workload import WorkloadBase, WorkloadError
from events.actions import ActionEvents
from literals import (
    ACME_MOUNT,
    ACME_ROLE_NAME,
    AUTOUNSEAL_MOUNT_PATH,
    AUTOUNSEAL_PROVIDES_RELATION_NAME,
    AUTOUNSEAL_REQUIRES_RELATION_NAME,
    CONFIG_TEMPLATE_DIR_PATH,
    CONFIG_TEMPLATE_NAME,
    CONTAINER_NAME,
    GRAFANA_DASHBOARDS_PATH,
    HSM_CONFIG_SECRET_ID_CONFIG_KEY,
    INGRESS_PER_APP_RELATION_NAME,
    INGRESS_PER_UNIT_RELATION_NAME,
    KV_RELATION_NAME,
    LOG_FORWARDING_RELATION_NAME,
    LOG_LEVELS,
    OPENBAO_CHARM_APPROLE_SECRET_LABEL,
    OPENBAO_CLUSTER_PORT,
    OPENBAO_PORT,
    PEER_RELATION_NAME,
    PKI_MOUNT,
    PKI_RELATION_NAME,
    PKI_ROLE_NAME,
    PKI_SELF_SIGNED_CA_DEFAULT_VALIDITY_HOURS,
    PROMETHEUS_ALERT_RULES_PATH,
    RESOURCE_CONFIG_KEYS,
    S3_RELATION_NAME,
    TLS_CERTIFICATE_ACCESS_RELATION_NAME,
    TLS_CERTIFICATES_ACME_RELATION_NAME,
    TLS_CERTIFICATES_PKI_RELATION_NAME,
    Substrate,
)
from managers.acme import ACMEManager
from managers.autounseal import AutounsealProviderManager, AutounsealRequirerManager
from managers.hsm import HSMManager
from managers.kv import KVManager
from managers.pki import PKIManager
from managers.tls import File, TLSManager
from workload_k8s import OpenBaoK8sWorkload
from workload_vm import OpenBaoVMWorkload

logger = logging.getLogger(__name__)


@trace_charm(
    tracing_endpoint="_tracing_endpoint",
    server_cert="_tracing_server_cert",
    extra_types=(TLSCertificatesProvidesV4,),
)
class OpenBaoCharm(CharmBase):
    """Charm for OpenBao on Kubernetes and on machines."""

    def __init__(self, *args: Any):
        super().__init__(*args)
        self.juju_facade = JujuFacade(self)
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            self.substrate = Substrate.K8S
            self.workload: WorkloadBase = OpenBaoK8sWorkload(
                self.unit.get_container(CONTAINER_NAME)
            )
        else:
            self.substrate = Substrate.VM
            self.workload = OpenBaoVMWorkload()
        self.unit.set_ports(OPENBAO_PORT)
        self._setup_observability()
        self.tls = TLSManager(
            charm=self,
            workload=self.workload,
            common_name=self._bind_address or "",
            sans_dns=self._access_sans_dns(),
            sans_ip=self._access_sans_ip(),
            country_name=self._config("access_country_name"),
            state_or_province_name=self._config("access_state_or_province_name"),
            locality_name=self._config("access_locality_name"),
            organization=self._config("access_organization"),
            organizational_unit=self._config("access_organizational_unit"),
            email_address=self._config("access_email_address"),
        )
        self.openbao_kv = VaultKvProvides(self, KV_RELATION_NAME)
        self.openbao_pki = TLSCertificatesProvidesV4(
            charm=self,
            relationship_name=PKI_RELATION_NAME,
        )
        pki_certificate_request = self._get_ca_certificate_request("pki")
        self.tls_certificates_pki = TLSCertificatesRequiresV4(
            charm=self,
            relationship_name=TLS_CERTIFICATES_PKI_RELATION_NAME,
            certificate_requests=[pki_certificate_request] if pki_certificate_request else [],
            mode=Mode.APP,
            refresh_events=[self.on.config_changed],
        )
        acme_certificate_request = self._get_ca_certificate_request("acme")
        self.tls_certificates_acme = TLSCertificatesRequiresV4(
            charm=self,
            relationship_name=TLS_CERTIFICATES_ACME_RELATION_NAME,
            certificate_requests=[acme_certificate_request] if acme_certificate_request else [],
            mode=Mode.APP,
            refresh_events=[self.on.config_changed],
        )
        self.openbao_autounseal_provides = OpenBaoAutounsealProvides(
            self, AUTOUNSEAL_PROVIDES_RELATION_NAME
        )
        self.openbao_autounseal_requires = OpenBaoAutounsealRequires(
            self, AUTOUNSEAL_REQUIRES_RELATION_NAME
        )
        self.hsm = HSMManager(self, self.workload, self.openbao_autounseal_requires)
        self.ingress_per_app = IngressPerAppRequirer(
            charm=self,
            relation_name=INGRESS_PER_APP_RELATION_NAME,
            port=OPENBAO_PORT,
            strip_prefix=True,
            scheme=lambda: "https",
            redirect_https=True,
        )
        self.ingress_per_unit = IngressPerUnitRequirer(
            self,
            relation_name=INGRESS_PER_UNIT_RELATION_NAME,
            port=OPENBAO_PORT,
            strip_prefix=True,
            redirect_https=True,
            scheme=lambda: "https",
        )
        self.s3_requirer = S3Requirer(self, S3_RELATION_NAME)

        configure_events = [
            self.on.config_changed,
            self.on.update_status,
            self.on.upgrade_charm,
            self.on.secret_changed,
            self.on.openbao_pebble_ready,
            self.on[PEER_RELATION_NAME].relation_created,
            self.on[PEER_RELATION_NAME].relation_changed,
            self.on.openbao_pki_relation_changed,
            self.on.tls_certificates_pki_relation_joined,
            self.tls_certificates_pki.on.certificate_available,
            self.openbao_autounseal_requires.on.openbao_autounseal_details_ready,
            self.openbao_autounseal_provides.on.openbao_autounseal_requirer_relation_created,
            self.openbao_autounseal_requires.on.openbao_autounseal_provider_relation_broken,
            self.openbao_autounseal_provides.on.openbao_autounseal_requirer_relation_broken,
            self.openbao_kv.on.new_vault_kv_client_attached,
        ]
        for event in configure_events:
            self.framework.observe(event, self._configure)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(
            self.openbao_kv.on.vault_kv_client_detached, self._on_openbao_kv_client_detached
        )
        self.action_events = ActionEvents(self)

    def _setup_observability(self) -> None:
        """Integrate with COS: directly on Kubernetes, through the COS agent on machines."""
        self.tracing = TracingEndpointRequirer(self, protocols=["otlp_http"])
        self._tracing_endpoint, self._tracing_server_cert = charm_tracing_config(
            self.tracing, cert_path=None
        )
        if self.substrate == Substrate.VM:
            self.resources_patch = None
            self._cos_agent = COSAgentProvider(
                self,
                refresh_events=[self.on[PEER_RELATION_NAME].relation_changed],
                scrape_configs=self.generate_openbao_scrape_configs,
                dashboard_dirs=[GRAFANA_DASHBOARDS_PATH],
                metrics_rules_dir=PROMETHEUS_ALERT_RULES_PATH,
            )
            return
        self.resources_patch = KubernetesComputeResourcesPatch(
            self, CONTAINER_NAME, resource_reqs_func=self._resource_reqs_from_config
        )
        self.grafana_dashboards = GrafanaDashboardProvider(self)
        self._metrics_endpoint = MetricsEndpointProvider(
            self,
            jobs=[
                {
                    "scheme": "https",
                    "tls_config": {"insecure_skip_verify": True},
                    "metrics_path": "/v1/sys/metrics",
                    "static_configs": [{"targets": [f"*:{OPENBAO_PORT}"]}],
                }
            ],
            alert_rules_path=PROMETHEUS_ALERT_RULES_PATH,
        )
        self._logging = LogForwarder(charm=self, relation_name=LOG_FORWARDING_RELATION_NAME)

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

    def _resource_reqs_from_config(self) -> ResourceRequirements:
        limits = {
            "cpu": self._config("cpu-limit"),
            "memory": self._config("memory-limit"),
        }
        requests = {
            "cpu": self._config("cpu-request"),
            "memory": self._config("memory-request"),
        }
        return adjust_resource_requirements(
            {k: v for k, v in limits.items() if v},
            {k: v for k, v in requests.items() if v},
            adhere_to_requests=True,
        )

    def _get_resources_patch_status(self) -> StatusBase | None:
        """Return why the Kubernetes resource limits are not applied yet, if they are not."""
        if (
            not self.resources_patch
            or not any(self._config(key) for key in RESOURCE_CONFIG_KEYS)
            or self.resources_patch.is_ready()
        ):
            return None
        if isinstance(self.resources_patch.get_status(), BlockedStatus):
            return BlockedStatus(
                "Failed to apply resources patch. Please monitor the logs for errors."
            )
        return WaitingStatus(
            "Waiting for resources patch to be ready. Please monitor the logs for errors."
        )

    def _on_install(self, event: InstallEvent) -> None:
        """Handle the install event."""
        if self.substrate == Substrate.K8S:
            if not self.workload.is_accessible():
                event.defer()
                return
            # The volume claim of a removed pod is kept and reused by the next pod
            # with the same ordinal, so a new unit must not inherit its raft data.
            self._delete_openbao_data()
        self._configure(event)

    def _on_collect_status(self, event: CollectStatusEvent) -> None:
        """Handle the collect status event."""
        event.add_status(self._get_unit_status())

    def _get_unit_status(self) -> StatusBase:  # noqa: C901
        """Return the status of the first unmet requirement, or Active."""
        if status := self._get_resources_patch_status():
            return status
        if config_error := self._get_config_error():
            return BlockedStatus(config_error)
        if self.hsm.requested and self.substrate == Substrate.K8S:
            return BlockedStatus("PKCS#11 HSM auto-unseal is only supported on machines")
        if hsm_error := self.hsm.get_config_error():
            return BlockedStatus(hsm_error)
        if not self.workload.is_accessible():
            return WaitingStatus("Waiting to be able to connect to openbao unit")
        if not self.juju_facade.relation_exists(PEER_RELATION_NAME):
            return WaitingStatus("Waiting for peer relation")
        if not self.api_address:
            return WaitingStatus("Waiting for bind address")
        if not self.unit.is_leader() and not self._other_peer_node_api_addresses():
            return WaitingStatus("Waiting for other units to provide their addresses")
        if not self.tls.tls_file_available_in_charm(File.CA):
            return WaitingStatus("Waiting for CA certificate to be accessible in the charm")
        if not self.tls.tls_file_pushed_to_workload(File.CA):
            return WaitingStatus("Waiting for CA certificate in workload")
        if not self.workload.is_running():
            return WaitingStatus("Waiting for OpenBao service to start")
        openbao = OpenBaoClient(
            url=self.api_address, ca_cert_path=self.tls.get_tls_file_path_in_charm(File.CA)
        )
        if not openbao.is_api_available():
            return WaitingStatus("Waiting for OpenBao API to be available")
        if not openbao.is_initialized():
            if openbao.is_seal_type_transit() or openbao.get_seal_type() == "pkcs11":
                return BlockedStatus("Please initialize OpenBao")
            return BlockedStatus(
                "Please initialize OpenBao or integrate with an auto-unseal provider"
            )
        try:
            if openbao.is_sealed():
                if openbao.needs_migration():
                    return BlockedStatus("Please migrate OpenBao")
                if openbao.is_seal_type_transit():
                    return WaitingStatus("Waiting for transit auto-unseal")
                if openbao.get_seal_type() == "pkcs11":
                    return WaitingStatus("Waiting for PKCS#11 auto-unseal")
                return BlockedStatus("Please unseal OpenBao")
        except OpenBaoClientError:
            return MaintenanceStatus("Seal check failed, waiting for OpenBao to recover")
        if not (approle := self._get_approle_auth_secret()):
            return BlockedStatus("Please authorize charm (see `authorize-charm` action)")
        try:
            if not openbao.authenticate(approle):
                return WaitingStatus("Waiting for OpenBao to become available")
        except OpenBaoAuthenticationError:
            return BlockedStatus("Please authorize charm (see `authorize-charm` action)")
        if not openbao.is_active_or_standby():
            return WaitingStatus("Waiting for openbao to finish raft leader election")
        return ActiveStatus()

    def _get_config_error(self) -> str | None:
        """Return why the config is invalid for the current integrations, if it is."""
        if self.juju_facade.relation_exists(TLS_CERTIFICATE_ACCESS_RELATION_NAME):
            if not sans_dns_config_is_valid(self._config("access_sans_dns")):
                return "Config value for access_sans_dns is not valid, it must be a comma separated list"
            if not sans_ip_config_is_valid(self._config("access_sans_ip")):
                return "Config value for access_sans_ip is not valid, it must be a comma separated list of IP addresses"
        if self.juju_facade.relation_exists(
            TLS_CERTIFICATES_PKI_RELATION_NAME
        ) or self.juju_facade.relation_exists(PKI_RELATION_NAME):
            if error := self._get_ca_config_error("pki", "PKI secrets engine"):
                return error
        if self.juju_facade.relation_exists(TLS_CERTIFICATES_ACME_RELATION_NAME):
            if error := self._get_ca_config_error("acme", "ACME server"):
                return error
        if not self._log_level_is_valid():
            return "log_level config is not valid"
        return None

    def _get_ca_config_error(self, prefix: str, feature: str) -> str | None:
        """Return why the config of the `pki` or `acme` intermediate CA is invalid, if it is."""
        if not common_name_config_is_valid(self._config(f"{prefix}_ca_common_name")):
            return f"{prefix}_ca_common_name is not set in the charm config, cannot configure {feature}"
        if not allowed_domains_config_is_valid(self._config(f"{prefix}_allowed_domains")):
            return f"Config value for {prefix}_allowed_domains is not valid, it must be a comma separated list"
        if not sans_dns_config_is_valid(self._config(f"{prefix}_ca_sans_dns")):
            return f"Config value for {prefix}_ca_sans_dns is not valid, it must be a comma separated list"
        return None

    def _configure(self, _: EventBase) -> None:  # noqa: C901
        """Install and configure OpenBao, then sync the integrations that need OpenBao."""
        if not self.workload.is_accessible():
            return
        self.workload.configure_log_rotation(self._config("logrotate_frequency"))
        try:
            self.workload.install()
        except WorkloadError as e:
            logger.error("Failed to install OpenBao: %s", e)
            return
        if self._get_resources_patch_status():
            return
        if not self.juju_facade.relation_exists(PEER_RELATION_NAME):
            return
        if not self.api_address:
            return
        if not all(
            self.tls.tls_file_pushed_to_workload(file) for file in (File.CA, File.CERT, File.KEY)
        ):
            return
        if not self._log_level_is_valid():
            return
        # A unit that is not the leader can only join the cluster once another
        # unit has shared its address in the peer relation.
        if not self.juju_facade.is_leader and not self._other_peer_node_api_addresses():
            logger.debug("Not leader and no peers, waiting for a peer")
            return

        self._set_peer_relation_node_api_address()
        peer_addresses = self._other_peer_node_api_addresses()
        active_peer_addresses = self._active_peer_addresses(peer_addresses)
        config_changed, seal_changed = self._generate_openbao_config_file(
            retry_join_addresses=active_peer_addresses or peer_addresses
        )
        environment_changed = self.workload.configure_service(
            self._get_openbao_autounseal_token(), self._juju_proxy_environment
        )
        # A raft node completes its join to the cluster using the retry_join
        # targets read at startup, so restart an uninitialized node when the
        # config changes to pick up the current active node. Restarting is
        # harmless at that point since the node holds no data. Without an
        # active node there is nothing to join, and a restart could only
        # interrupt an initialization or unseal in progress. A seal type
        # change also requires a restart so OpenBao can pick up PKCS#11 or
        # transit auto-unseal. Any PKCS#11 config rewrite (secret/lib ready or
        # updated) must restart so the new seal stanza is read.
        restart_needed = (
            environment_changed
            or seal_changed
            or (config_changed and self.hsm.requested)
            or (
                config_changed and bool(active_peer_addresses) and self._openbao_is_uninitialized()
            )
        )
        if restart_needed and self.workload.is_running():
            self.workload.restart()
        else:
            try:
                self.workload.start()
            except WorkloadError as e:
                logger.error("Failed to start OpenBao service: %s", e)
                return

        if (openbao := self.get_openbao_client()) and self._is_sealed_with_auto_unseal(openbao):
            logger.info("OpenBao is sealed with an auto-unseal type, restarting to trigger unseal")
            self.workload.restart()
            return

        if not (openbao := self.get_authenticated_openbao_client()):
            return
        pki_manager = self._get_pki_manager(openbao)
        if pki_manager:
            pki_manager.configure()
        if acme_manager := self._get_acme_manager(openbao):
            acme_manager.configure()
        self._sync_openbao_autounseal(openbao)
        self._sync_openbao_kv(openbao)
        if pki_manager:
            pki_manager.sync()

        if openbao.is_active() and not openbao.is_raft_cluster_healthy():
            logger.warning("Raft cluster is not healthy: %s", openbao.get_raft_cluster_state())

    @staticmethod
    def _is_sealed_with_auto_unseal(openbao: OpenBaoClient) -> bool:
        """Return whether OpenBao is sealed although it unseals itself on start."""
        try:
            return (
                openbao.is_api_available()
                and openbao.is_initialized()
                and openbao.is_sealed()
                and (openbao.is_seal_type_transit() or openbao.get_seal_type() == "pkcs11")
            )
        except OpenBaoClientError:
            return False

    def _on_remove(self, _: EventBase) -> None:
        """Remove the node from the raft cluster, then stop OpenBao and delete its data."""
        if not self.workload.is_accessible():
            return
        self._remove_node_from_raft_cluster()
        if self.workload.is_running():
            try:
                self.workload.stop()
            except WorkloadError:
                logger.warning("Failed to stop OpenBao service")
        self._delete_openbao_data()

    def _remove_node_from_raft_cluster(self) -> None:
        """Remove the node from the raft cluster."""
        if not (approle := self._get_approle_auth_secret()):
            logger.error("Failed to authenticate to OpenBao")
            return
        if not self.api_address:
            logger.error("Can't remove node from cluster - OpenBao API address is not available")
            return
        openbao = OpenBaoClient(url=self.api_address, ca_cert_path=None)
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
        if openbao.is_node_in_raft_peers(id=self.node_id) and openbao.get_num_raft_peers() > 1:
            openbao.remove_raft_node(id=self.node_id)

    def _delete_openbao_data(self) -> None:
        """Delete OpenBao's data."""
        for path, name in (
            (f"{self.workload.raft_storage_path}/vault.db", "main"),
            (f"{self.workload.raft_storage_path}/raft/raft.db", "Raft"),
        ):
            try:
                self.workload.remove_path(path=path)
                logger.info("Removed OpenBao's %s database", name)
            except ValueError:
                logger.info("No OpenBao %s database to remove", name)

    def _on_openbao_kv_client_detached(self, event: VaultKvClientDetachedEvent) -> None:
        KVManager.remove_unit_credentials(self.juju_facade, event.unit_name)

    def _get_ca_certificate_request(self, prefix: str) -> CertificateRequestAttributes | None:
        """Return the CSR attributes of the `pki` or `acme` intermediate CA, if configured."""
        common_name = self._config(f"{prefix}_ca_common_name")
        if not common_name:
            logger.warning("%s_ca_common_name is not set in the charm config", prefix)
            return None
        sans_dns = self._config(f"{prefix}_ca_sans_dns")
        if not sans_dns_config_is_valid(sans_dns):
            logger.warning("%s_ca_sans_dns is not valid", prefix)
            return None
        return CertificateRequestAttributes(
            common_name=common_name,
            sans_dns=frozenset(name.strip() for name in sans_dns.split(","))
            if sans_dns
            else frozenset(),
            country_name=self._config(f"{prefix}_ca_country_name") or None,
            state_or_province_name=self._config(f"{prefix}_ca_state_or_province_name") or None,
            locality_name=self._config(f"{prefix}_ca_locality_name") or None,
            organization=self._config(f"{prefix}_ca_organization") or None,
            organizational_unit=self._config(f"{prefix}_ca_organizational_unit") or None,
            email_address=self._config(f"{prefix}_ca_email_address") or None,
            is_ca=True,
        )

    def _get_pki_manager(self, openbao: OpenBaoClient) -> PKIManager | None:
        """Return the manager of the PKI secrets engine, if the config allows one."""
        if error := self._get_ca_config_error("pki", "PKI secrets engine"):
            logger.warning("Skipping PKI secrets engine configuration: %s", error)
            return None
        if not (certificate_request := self._get_ca_certificate_request("pki")):
            return None
        return PKIManager(
            charm=self,
            openbao_client=openbao,
            certificate_request_attributes=certificate_request,
            mount_point=PKI_MOUNT,
            role_name=PKI_ROLE_NAME,
            openbao_pki=self.openbao_pki,
            # Without the relation, PKIManager uses a self-signed CA.
            tls_certificates_pki=self.tls_certificates_pki
            if self.juju_facade.relation_exists(TLS_CERTIFICATES_PKI_RELATION_NAME)
            else None,
            allowed_domains=self._config("pki_allowed_domains"),
            allow_bare_domains=self.juju_facade.get_bool_config("pki_allow_bare_domains"),
            allow_subdomains=self.juju_facade.get_bool_config("pki_allow_subdomains"),
            allow_wildcard_certificates=self.juju_facade.get_bool_config(
                "pki_allow_wildcard_certificates"
            ),
            allow_any_name=self.juju_facade.get_bool_config("pki_allow_any_name"),
            allow_ip_sans=self.juju_facade.get_bool_config("pki_allow_ip_sans"),
            organization=self._config("pki_organization"),
            organizational_unit=self._config("pki_organizational_unit"),
            country=self._config("pki_country"),
            province=self._config("pki_province"),
            locality=self._config("pki_locality"),
            self_signed_ca_validity_hours=self.juju_facade.get_int_config(
                "pki_self_signed_ca_validity"
            )
            or PKI_SELF_SIGNED_CA_DEFAULT_VALIDITY_HOURS,
        )

    def _get_acme_manager(self, openbao: OpenBaoClient) -> ACMEManager | None:
        """Return the manager of the ACME server, if the config allows one."""
        if error := self._get_ca_config_error("acme", "ACME server"):
            logger.warning("Skipping ACME server configuration: %s", error)
            return None
        if not (certificate_request := self._get_ca_certificate_request("acme")):
            return None
        return ACMEManager(
            charm=self,
            openbao_client=openbao,
            mount_point=ACME_MOUNT,
            tls_certificates_acme=self.tls_certificates_acme,
            certificate_request_attributes=certificate_request,
            role_name=ACME_ROLE_NAME,
            openbao_address=f"https://{self._ingress_address}:{OPENBAO_PORT}",
            allowed_domains=self._config("acme_allowed_domains"),
            allow_bare_domains=self.juju_facade.get_bool_config("acme_allow_bare_domains"),
            allow_subdomains=self.juju_facade.get_bool_config("acme_allow_subdomains"),
            allow_wildcard_certificates=self.juju_facade.get_bool_config(
                "acme_allow_wildcard_certificates"
            ),
            allow_any_name=self.juju_facade.get_bool_config("acme_allow_any_name"),
            allow_ip_sans=self.juju_facade.get_bool_config("acme_allow_ip_sans"),
            organization=self._config("acme_organization"),
            organizational_unit=self._config("acme_organizational_unit"),
            country=self._config("acme_country"),
            province=self._config("acme_province"),
            locality=self._config("acme_locality"),
        )

    def _sync_openbao_autounseal(self, openbao_client: OpenBaoClient) -> None:
        """Send credentials to the auto-unseal requirers that miss them."""
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
        for kv_request in self.openbao_kv.get_kv_requests():
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

    def get_openbao_client(self) -> OpenBaoClient | None:
        """Return a client for the OpenBao service of this unit, if it can be reached."""
        if not self.api_address or not self.tls.tls_file_available_in_charm(File.CA):
            return None
        return OpenBaoClient(
            url=self.api_address,
            ca_cert_path=self.tls.get_tls_file_path_in_charm(File.CA),
        )

    def get_authenticated_openbao_client(self) -> OpenBaoClient | None:
        """Return a client for this unit authenticated with the charm's AppRole.

        Returns None if OpenBao is not available, or neither active nor standby,
        or the charm has not been authorized.
        """
        openbao = self.get_openbao_client()
        if not openbao or not openbao.is_api_available():
            return None
        if not self._authenticate_openbao_client(openbao):
            return None
        if not openbao.is_active_or_standby():
            return None
        return openbao

    def get_active_openbao_client(self) -> OpenBaoClient | None:
        """Return an authenticated client for the _active_ OpenBao node.

        This may not be the OpenBao service running on this unit.
        """
        for address in [self.api_address, *self._other_peer_node_api_addresses()]:
            if not address:
                continue
            try:
                openbao = OpenBaoClient(
                    address, ca_cert_path=self.tls.get_tls_file_path_in_charm(File.CA)
                )
            except (OpenBaoCertsError, TransientJujuError) as e:
                logger.warning("Failed to get OpenBao client: %s", e)
                continue
            if openbao.is_active():
                if not openbao.is_api_available() or not self._authenticate_openbao_client(
                    openbao
                ):
                    return None
                return openbao
        return None

    def _authenticate_openbao_client(self, openbao: OpenBaoClient) -> bool:
        """Authenticate the client with the charm's AppRole.

        Returns:
            bool: Whether the OpenBao client authentication was successful.
        """
        if not (approle := self._get_approle_auth_secret()):
            return False
        try:
            return openbao.authenticate(approle)
        except OpenBaoAuthenticationError:
            logger.warning(
                "OpenBao rejected the charm's credentials. "
                "Removing stored credentials to prevent lockout."
            )
            self.juju_facade.remove_secret(label=OPENBAO_CHARM_APPROLE_SECRET_LABEL)
            return False

    def _get_approle_auth_secret(self) -> AppRole | None:
        """Return the charm's AppRole credentials, or None if it has not been authorized."""
        try:
            role_id, secret_id = self.juju_facade.get_secret_content_values(
                "role-id", "secret-id", label=OPENBAO_CHARM_APPROLE_SECRET_LABEL, refresh=True
            )
        except NoSuchSecretError:
            logger.warning("Approle secret not yet created")
            return None
        return AppRole(role_id, secret_id) if role_id and secret_id else None

    def _active_peer_addresses(self, addresses: List[str]) -> List[str]:
        """Return the addresses of the active node, if one can be identified.

        OpenBao, unlike Vault, does not forward raft bootstrap answers received
        by standby nodes to the active node, and it answers the challenge of
        whichever retry_join target responds first. A new node can therefore
        only join the cluster when retry_join points at the active node. No
        node is active before the cluster is initialized, or when it is
        unreachable.
        """
        try:
            ca_path = self.tls.get_tls_file_path_in_charm(File.CA)
        except (OpenBaoCertsError, TransientJujuError):
            logger.debug("CA certificate unavailable, cannot find the active peer")
            return []
        active_addresses = []
        for address in addresses:
            try:
                if OpenBaoClient(address, ca_cert_path=ca_path, timeout=5).is_active():
                    active_addresses.append(address)
            except Exception:
                logger.debug("Failed to probe %s for active status", address, exc_info=True)
        logger.info("Active peers for retry_join: %s (out of %s)", active_addresses, addresses)
        return active_addresses

    def _openbao_is_uninitialized(self) -> bool:
        """Return whether the local OpenBao node is not yet part of a cluster.

        A node that has not completed its raft join is reported as not
        initialized. A node that is wedged answering a raft join (its API does
        not respond) is also treated as uninitialized, since restarting it is
        the only way to make it pick up new retry_join targets.
        """
        if not self.api_address:
            return False
        try:
            ca_path = self.tls.get_tls_file_path_in_charm(File.CA)
        except (OpenBaoCertsError, TransientJujuError):
            return False
        client = OpenBaoClient(url=self.api_address, ca_cert_path=ca_path, timeout=5)
        try:
            if not client.is_api_available():
                # The service is running but its API does not respond: either
                # it is still starting up or it is wedged mid raft-join.
                return True
            return not client.is_initialized()
        except OpenBaoClientError:
            return False

    def _generate_openbao_config_file(self, retry_join_addresses: List[str]) -> tuple[bool, bool]:
        """Render the OpenBao config file and push it to the workload if it changed.

        Args:
            retry_join_addresses: API addresses of the raft nodes to join.

        Returns:
            A tuple of (content_changed, seal_changed).
        """
        assert self.api_address and self._cluster_address
        tls_directory = self.workload.tls_directory_path
        retry_joins = [
            {"leader_api_addr": address, "leader_ca_cert_file": f"{tls_directory}/ca.pem"}
            for address in retry_join_addresses
        ]
        content = render_openbao_config_file(
            config_template_path=CONFIG_TEMPLATE_DIR_PATH,
            config_template_name=CONFIG_TEMPLATE_NAME,
            default_lease_ttl=self._get_string_config_or_raise("default_lease_ttl"),
            max_lease_ttl=self._get_string_config_or_raise("max_lease_ttl"),
            cluster_address=self._cluster_address,
            api_address=self.api_address,
            tls_cert_file=f"{tls_directory}/cert.pem",
            tls_key_file=f"{tls_directory}/key.pem",
            tcp_address=f"[::]:{OPENBAO_PORT}",
            raft_storage_path=self.workload.raft_storage_path,
            node_id=self.node_id,
            retry_joins=retry_joins,
            autounseal_config=self._get_openbao_autounseal_configuration(),
            pkcs11_config=self.hsm.get_seal_configuration(),
            log_level=self._get_string_config_or_raise("log_level"),
        )
        config_file_path = self.workload.config_file_path
        existing_content = ""
        if self.workload.exists(path=config_file_path):
            existing_content = self.workload.pull(path=config_file_path).read()
        seal_changed = seal_type_has_changed(existing_content, content)
        if config_file_content_matches(existing_content=existing_content, new_content=content):
            return False, seal_changed
        self.workload.push(path=config_file_path, source=content)
        return True, seal_changed

    def _get_string_config_or_raise(self, key: str) -> str:
        """Return a string config option that has a default value."""
        value = self.config.get(key)
        if not value or not isinstance(value, str):
            raise ValueError(f"Invalid config {key}")
        return value

    def _log_level_is_valid(self) -> bool:
        return self.config.get("log_level") in LOG_LEVELS

    def _get_openbao_autounseal_token(self) -> str | None:
        autounseal_relation_details = self.openbao_autounseal_requires.get_details()
        if not autounseal_relation_details:
            return None
        autounseal_requirer_manager = AutounsealRequirerManager(
            self, self.openbao_autounseal_requires
        )
        return autounseal_requirer_manager.get_provider_openbao_token(
            autounseal_relation_details, self.tls.get_tls_file_path_in_charm(File.AUTOUNSEAL_CA)
        )

    def _get_openbao_autounseal_configuration(self) -> AutounsealConfiguration | None:
        autounseal_relation_details = self.openbao_autounseal_requires.get_details()
        if not autounseal_relation_details:
            return None
        if self.hsm.requested:
            logger.warning(
                "Ignoring transit auto-unseal because %s is set",
                HSM_CONFIG_SECRET_ID_CONFIG_KEY,
            )
            return None
        self.tls.push_autounseal_ca_cert(autounseal_relation_details.ca_certificate)
        return AutounsealConfiguration(
            autounseal_relation_details.address,
            autounseal_relation_details.mount_path,
            autounseal_relation_details.key_name,
            self.tls.get_tls_file_path_in_workload(File.AUTOUNSEAL_CA),
        )

    @property
    def _juju_proxy_environment(self) -> dict[str, str]:
        """Return the HTTP_PROXY, HTTPS_PROXY and NO_PROXY values of the Juju model."""
        env = {}
        if http_proxy := get_env_var("JUJU_CHARM_HTTP_PROXY"):
            env["HTTP_PROXY"] = http_proxy
        if https_proxy := get_env_var("JUJU_CHARM_HTTPS_PROXY"):
            env["HTTPS_PROXY"] = https_proxy
        if no_proxy := get_env_var("JUJU_CHARM_NO_PROXY"):
            env["NO_PROXY"] = no_proxy
        return env

    def _access_sans_dns(self) -> frozenset[str]:
        """Return the DNS names of this unit's certificate."""
        access_sans_dns = self._config("access_sans_dns")
        if not access_sans_dns:
            return frozenset({socket.getfqdn()})
        if not sans_dns_config_is_valid(access_sans_dns):
            logger.warning("access_sans_dns is not valid, it must be a comma separated list")
            return frozenset()
        return frozenset(
            {socket.getfqdn(), *(name.strip() for name in access_sans_dns.split(","))}
        )

    def _access_sans_ip(self) -> frozenset[str]:
        """Return the IP addresses of this unit's certificate."""
        ip_addresses = set(self.juju_facade.get_ingress_addresses(PEER_RELATION_NAME))
        if self._bind_address:
            ip_addresses.add(self._bind_address)
        access_sans_ip = self._config("access_sans_ip")
        if access_sans_ip and not sans_ip_config_is_valid(access_sans_ip):
            logger.warning(
                "access_sans_ip is not valid, it must be a comma separated list of IP addresses"
            )
        elif access_sans_ip:
            ip_addresses.update(ip.strip() for ip in access_sans_ip.split(","))
        return frozenset(ip_addresses)

    def _get_relation_api_address(self, relation: Relation) -> str | None:
        """Return the API address of this unit on the network of the relation."""
        if not (ingress_address := self.juju_facade.get_ingress_address(relation=relation)):
            return None
        return f"https://{ingress_address}:{OPENBAO_PORT}"

    def _set_peer_relation_node_api_address(self) -> None:
        """Set the unit address in the peer relation."""
        assert self.api_address
        self.juju_facade.set_unit_relation_data(
            data={"node_api_address": self.api_address},
            name=PEER_RELATION_NAME,
        )

    def _other_peer_node_api_addresses(self) -> List[str]:
        """Return the API addresses that the other units shared in the peer relation."""
        return [
            databag["node_api_address"]
            for databag in self.juju_facade.get_remote_units_relation_data(name=PEER_RELATION_NAME)
            if databag.get("node_api_address") not in (None, self.api_address)
        ]

    def _config(self, key: str) -> str:
        """Return a string config option, or an empty string if it is not set."""
        return self.juju_facade.get_string_config(key)

    @property
    def _bind_address(self) -> str | None:
        return self.juju_facade.get_bind_address(relation_name=PEER_RELATION_NAME)

    @property
    def _ingress_address(self) -> str | None:
        return self.juju_facade.get_ingress_address(PEER_RELATION_NAME)

    @property
    def _host(self) -> str | None:
        """Return the host other units and clients reach this unit at.

        The unit is reachable once Juju has bound its peer network. Pods get a
        new IP when they are rescheduled, so on Kubernetes the stable pod FQDN
        is used, e.g. openbao-0.openbao-endpoints.model.svc.cluster.local.
        """
        if not self._bind_address:
            return None
        return socket.getfqdn() if self.substrate == Substrate.K8S else self._bind_address

    @property
    def api_address(self) -> str | None:
        """Return the API address of this unit, e.g. https://1.2.3.4:8200."""
        return f"https://{self._host}:{OPENBAO_PORT}" if self._host else None

    @property
    def _cluster_address(self) -> str | None:
        """Return the cluster address of this unit, e.g. https://1.2.3.4:8201."""
        return f"https://{self._host}:{OPENBAO_CLUSTER_PORT}" if self._host else None

    @property
    def node_id(self) -> str:
        """Return the raft node id of this unit, e.g. my-model-openbao-0."""
        return f"{self.model.name}-{self.unit.name}"


if __name__ == "__main__":  # pragma: nocover
    main(OpenBaoCharm)
