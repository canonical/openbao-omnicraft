#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import patch

import ops.testing as testing
import pytest
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    StatusBase,
    WaitingStatus,
)

from common.openbao_autounseal import AutounsealDetails
from common.openbao_client import OpenBaoAuthenticationError, OpenBaoClientError
from fixtures import OpenBaoCharmFixtures, k8s_only, peer_network, vm_only

NOT_A_LIST = "not a comma separated list"
HSM_SECRET_CONTENT = {"slot": "0", "pin": "1234", "key-label": "bao-root-key"}
RESOURCES_PATCH = "charms.observability_libs.v0.kubernetes_compute_resources_patch.KubernetesComputeResourcesPatch"


class TestCharmCollectUnitStatus(OpenBaoCharmFixtures):
    def collect_status(self, state: testing.State):
        return self.ctx.run(self.ctx.on.collect_unit_status(), state).unit_status

    @pytest.mark.parametrize(
        "endpoint, config, message",
        [
            (
                "tls-certificates-access",
                {"access_sans_dns": NOT_A_LIST},
                "Config value for access_sans_dns is not valid, it must be a comma separated list",
            ),
            (
                "tls-certificates-access",
                {"access_sans_ip": "not an ip, also bad"},
                "Config value for access_sans_ip is not valid, it must be a comma separated list of IP addresses",
            ),
            (
                "tls-certificates-pki",
                {},
                "pki_ca_common_name is not set in the charm config, cannot configure PKI secrets engine",
            ),
            (
                "openbao-pki",
                {},
                "pki_ca_common_name is not set in the charm config, cannot configure PKI secrets engine",
            ),
            (
                "tls-certificates-pki",
                {"pki_ca_common_name": "domain.com", "pki_allowed_domains": NOT_A_LIST},
                "Config value for pki_allowed_domains is not valid, it must be a comma separated list",
            ),
            (
                "tls-certificates-pki",
                {"pki_ca_common_name": "domain.com", "pki_ca_sans_dns": NOT_A_LIST},
                "Config value for pki_ca_sans_dns is not valid, it must be a comma separated list",
            ),
            (
                "tls-certificates-acme",
                {},
                "acme_ca_common_name is not set in the charm config, cannot configure ACME server",
            ),
            (
                "tls-certificates-acme",
                {"acme_ca_common_name": "domain.com", "acme_allowed_domains": NOT_A_LIST},
                "Config value for acme_allowed_domains is not valid, it must be a comma separated list",
            ),
            (
                "tls-certificates-acme",
                {"acme_ca_common_name": "domain.com", "acme_ca_sans_dns": NOT_A_LIST},
                "Config value for acme_ca_sans_dns is not valid, it must be a comma separated list",
            ),
            (None, {"log_level": "not valid"}, "log_level config is not valid"),
        ],
    )
    def test_given_invalid_config_when_collect_unit_status_then_status_is_blocked(
        self, endpoint: str | None, config: dict[str, str | int | float | bool], message: str
    ):
        relations = [testing.Relation(endpoint=endpoint)] if endpoint else []
        state_in = testing.State(config=config, relations=relations)

        assert self.collect_status(state_in) == BlockedStatus(message)

    def test_given_workload_not_accessible_when_collect_unit_status_then_status_is_waiting(self):
        self.mock_workload.is_accessible.return_value = False

        assert self.collect_status(self.ready_state()) == WaitingStatus(
            "Waiting to be able to connect to openbao unit"
        )

    def test_given_peer_relation_not_created_when_collect_unit_status_then_status_is_waiting(self):
        assert self.collect_status(self.ready_state(relations=[])) == WaitingStatus(
            "Waiting for peer relation"
        )

    def test_given_bind_address_not_available_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        state_in = self.ready_state(networks=[peer_network("")])

        assert self.collect_status(state_in) == WaitingStatus("Waiting for bind address")

    def test_given_non_leader_without_peer_addresses_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        assert self.collect_status(self.ready_state(leader=False)) == WaitingStatus(
            "Waiting for other units to provide their addresses"
        )

    def test_given_ca_not_available_in_charm_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        self.set_openbao_ready()
        self.mock_tls.tls_file_available_in_charm.return_value = False

        assert self.collect_status(self.ready_state()) == WaitingStatus(
            "Waiting for CA certificate to be accessible in the charm"
        )

    def test_given_ca_not_pushed_to_workload_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        self.set_openbao_ready()
        self.mock_tls.tls_file_pushed_to_workload.return_value = False

        assert self.collect_status(self.ready_state()) == WaitingStatus(
            "Waiting for CA certificate in workload"
        )

    def test_given_service_not_running_when_collect_unit_status_then_status_is_waiting(self):
        self.set_openbao_ready()
        self.mock_workload.is_running.return_value = False

        assert self.collect_status(self.ready_state()) == WaitingStatus(
            "Waiting for OpenBao service to start"
        )

    @pytest.mark.parametrize(
        "openbao, status",
        [
            (
                {"is_api_available": False},
                WaitingStatus("Waiting for OpenBao API to be available"),
            ),
            (
                {"is_initialized": False},
                BlockedStatus(
                    "Please initialize OpenBao or integrate with an auto-unseal provider"
                ),
            ),
            (
                {"is_initialized": False, "is_seal_type_transit": True},
                BlockedStatus("Please initialize OpenBao"),
            ),
            (
                {"is_initialized": False, "get_seal_type": "pkcs11"},
                BlockedStatus("Please initialize OpenBao"),
            ),
            ({"is_sealed": True}, BlockedStatus("Please unseal OpenBao")),
            (
                {"is_sealed": True, "needs_migration": True},
                BlockedStatus("Please migrate OpenBao"),
            ),
            (
                {"is_sealed": True, "is_seal_type_transit": True},
                WaitingStatus("Waiting for transit auto-unseal"),
            ),
            (
                {"is_sealed": True, "get_seal_type": "pkcs11"},
                WaitingStatus("Waiting for PKCS#11 auto-unseal"),
            ),
            (
                {"is_sealed": OpenBaoClientError()},
                MaintenanceStatus("Seal check failed, waiting for OpenBao to recover"),
            ),
            ({"authenticate": False}, WaitingStatus("Waiting for OpenBao to become available")),
            (
                {"authenticate": OpenBaoAuthenticationError()},
                BlockedStatus("Please authorize charm (see `authorize-charm` action)"),
            ),
            (
                {"is_active_or_standby": False},
                WaitingStatus("Waiting for openbao to finish raft leader election"),
            ),
            ({}, ActiveStatus()),
        ],
    )
    def test_given_openbao_state_when_collect_unit_status_then_status_reflects_it(
        self, openbao: dict[str, object], status: StatusBase
    ):
        self.set_openbao_ready(**openbao)

        assert self.collect_status(self.ready_state()) == status

    def test_given_charm_not_authorized_when_collect_unit_status_then_status_is_blocked(self):
        self.set_openbao_ready()

        assert self.collect_status(self.ready_state(secrets=[])) == BlockedStatus(
            "Please authorize charm (see `authorize-charm` action)"
        )

    def test_given_non_leader_with_peer_addresses_when_collect_unit_status_then_status_is_active(
        self,
    ):
        self.set_openbao_ready()
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers", peers_data={1: {"node_api_address": "https://1.2.3.4:8200"}}
        )

        state_in = self.ready_state(leader=False, relations=[peer_relation])

        assert self.collect_status(state_in) == ActiveStatus()

    def test_given_openbao_pki_relation_without_external_ca_when_common_name_configured_then_status_is_active(
        self,
    ):
        self.set_openbao_ready()
        pki_relation = testing.Relation(endpoint="openbao-pki", interface="tls-certificates")

        state_in = self.ready_state(pki_relation, config={"pki_ca_common_name": "domain.com"})

        assert self.collect_status(state_in) == ActiveStatus()

    # Kubernetes resource limits

    @k8s_only
    @pytest.mark.parametrize(
        "patch_status, status",
        [
            (
                WaitingStatus("Some waiting status message"),
                WaitingStatus(
                    "Waiting for resources patch to be ready. Please monitor the logs for errors."
                ),
            ),
            (
                BlockedStatus("Some blocked status message"),
                BlockedStatus(
                    "Failed to apply resources patch. Please monitor the logs for errors."
                ),
            ),
        ],
    )
    def test_given_resources_patch_not_ready_when_collect_unit_status_then_status_reflects_it(
        self, patch_status: StatusBase, status: StatusBase
    ):
        with (
            patch(f"{RESOURCES_PATCH}.is_ready", return_value=False),
            patch(f"{RESOURCES_PATCH}.get_status", return_value=patch_status),
        ):
            state_out = self.collect_status(self.ready_state(config={"cpu-limit": "1"}))

        assert state_out == status

    @k8s_only
    def test_given_no_resource_config_when_resources_patch_not_ready_then_status_is_not_about_it(
        self,
    ):
        self.set_openbao_ready()
        with patch(f"{RESOURCES_PATCH}.is_ready", return_value=False):
            assert self.collect_status(self.ready_state()) == ActiveStatus()

    # PKCS#11 HSM

    @k8s_only
    def test_given_hsm_config_on_kubernetes_when_collect_unit_status_then_status_is_blocked(self):
        hsm_secret = testing.Secret(tracked_content=HSM_SECRET_CONTENT)
        state_in = testing.State(
            config={"hsm-config-secret-id": hsm_secret.id}, secrets=[hsm_secret]
        )

        assert self.collect_status(state_in) == BlockedStatus(
            "PKCS#11 HSM auto-unseal is only supported on machines"
        )

    @vm_only
    def test_given_hsm_secret_not_granted_when_collect_unit_status_then_status_is_blocked(self):
        state_in = testing.State(config={"hsm-config-secret-id": "secret:cqgj49fmp25c7796r0pg"})

        assert self.collect_status(state_in) == BlockedStatus(
            "hsm-config secret is not accessible; grant it to the charm with `juju grant-secret`"
        )

    @vm_only
    def test_given_hsm_secret_missing_fields_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        hsm_secret = testing.Secret(tracked_content={"pin": "1234"})
        state_in = testing.State(
            config={"hsm-config-secret-id": hsm_secret.id}, secrets=[hsm_secret]
        )

        assert self.collect_status(state_in) == BlockedStatus(
            "hsm-config secret is missing required fields: slot or token-label, key-label or key-id"
        )

    @vm_only
    def test_given_hsm_and_transit_autounseal_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        self.mock_autounseal_requires_get_details.return_value = AutounsealDetails(
            "1.2.3.4", "charm-autounseal", "key name", "role id", "secret id", "ca cert"
        )
        hsm_secret = testing.Secret(tracked_content=HSM_SECRET_CONTENT)
        state_in = testing.State(
            config={"hsm-config-secret-id": hsm_secret.id}, secrets=[hsm_secret]
        )

        assert self.collect_status(state_in) == BlockedStatus(
            "PKCS#11 HSM seal cannot be used together with transit auto-unseal"
        )

    @vm_only
    @pytest.mark.parametrize(
        "resource_content, installed_module, kms_plugin, message",
        [
            (
                b"placeholder",
                None,
                None,
                "hsm-lib resource is not attached; pack the PKCS#11 module and deps "
                "into a tarball and use `juju attach-resource openbao hsm-lib=./hsm-lib.tar.gz`",
            ),
            (
                b"\x7fELF" + b"\x00" * 16,
                None,
                None,
                "hsm-lib could not be installed; ensure the tarball contains the PKCS#11 "
                "module (name it pkcs11.so or set secret key `lib` to the module filename)",
            ),
            (
                b"\x7fELF" + b"\x00" * 16,
                "/var/snap/openbao/common/hsm/libykcs11.so",
                None,
                "OpenBao snap does not include the PKCS#11 KMS plugin "
                "(requires an amd64/arm64 snap revision that ships it)",
            ),
        ],
    )
    def test_given_hsm_lib_not_usable_when_collect_unit_status_then_status_is_blocked(
        self,
        tmp_path: Path,
        resource_content: bytes,
        installed_module: str | None,
        kms_plugin: tuple[str, str, str, str] | None,
        message: str,
    ):
        self.mock_workload.install_hsm_lib.return_value = installed_module
        self.mock_workload.pkcs11_kms_plugin.return_value = kms_plugin
        hsm_lib = tmp_path / "libykcs11.so"
        hsm_lib.write_bytes(resource_content)
        hsm_secret = testing.Secret(tracked_content=HSM_SECRET_CONTENT)
        state_in = testing.State(
            config={"hsm-config-secret-id": hsm_secret.id},
            secrets=[hsm_secret],
            resources=[testing.Resource(name="hsm-lib", path=hsm_lib)],
        )

        assert self.collect_status(state_in) == BlockedStatus(message)
