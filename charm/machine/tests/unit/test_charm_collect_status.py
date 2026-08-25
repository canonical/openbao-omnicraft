#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.


from pathlib import Path
from unittest.mock import MagicMock

import ops.testing as testing
import pytest
from charms.operator_libs_linux.v2.snap import Snap
from openbao.openbao_autounseal import AutounsealDetails
from openbao.openbao_client import OpenBaoClientError
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

from fixtures import OpenBaoCharmFixtures


class TestCharmCollectUnitStatus(OpenBaoCharmFixtures):
    def test_given_invalid_log_level_config_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        state_in = testing.State(
            config={"log_level": "not valid"},
        )
        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus("log_level config is not valid")

    def test_given_openbao_pki_relation_without_external_ca_when_common_name_configured_then_status_is_active(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(
                spec=Snap, revision="1.18/stable", services={"server": {"active": True}}
            )
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "is_sealed.return_value": False,
                "needs_migration.return_value": False,
                "is_seal_type_transit.return_value": False,
            },
        )
        pki_relation = testing.Relation(
            endpoint="openbao-pki",
            interface="tls-certificates",
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        approle_secret = testing.Secret(
            label="openbao-approle-auth-details",
            tracked_content={
                "role-id": "existing role id",
                "secret-id": "existing secret id",
            },
        )
        state_in = testing.State(
            config={"pki_ca_common_name": "domain.com"},
            relations=[pki_relation, peer_relation],
            leader=True,
            planned_units=3,
            secrets=[approle_secret],
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert isinstance(state_out.unit_status, ActiveStatus)

    def test_given_pki_tls_relation_and_bad_common_name_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        tls_relation = testing.Relation(
            endpoint="tls-certificates-pki",
            interface="tls-certificates",
        )
        state_in = testing.State(
            config={"pki_ca_common_name": ""},
            relations=[tls_relation],
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "pki_ca_common_name is not set in the charm config, cannot configure PKI secrets engine"
        )

    def test_given_pki_tls_relation_and_bad_allowed_domains_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        tls_relation = testing.Relation(
            endpoint="tls-certificates-pki",
            interface="tls-certificates",
        )
        state_in = testing.State(
            config={
                "pki_ca_common_name": "domain.com",
                "pki_allowed_domains": "not a comma separated list",
            },
            relations=[tls_relation],
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "Config value for pki_allowed_domains is not valid, it must be a comma separated list"
        )

    def test_given_pki_tls_relation_and_bad_sans_dns_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        tls_relation = testing.Relation(
            endpoint="tls-certificates-pki",
            interface="tls-certificates",
        )
        state_in = testing.State(
            config={
                "pki_ca_common_name": "domain.com",
                "pki_ca_sans_dns": "not a comma separated list",
            },
            relations=[tls_relation],
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "Config value for pki_ca_sans_dns is not valid, it must be a comma separated list"
        )

    def test_given_acme_tls_relation_and_bad_common_name_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        tls_relation = testing.Relation(
            endpoint="tls-certificates-acme",
            interface="tls-certificates",
        )
        state_in = testing.State(
            config={"acme_ca_common_name": ""},
            relations=[tls_relation],
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "acme_ca_common_name is not set in the charm config, cannot configure ACME server"
        )

    def test_given_acme_tls_relation_and_bad_allowed_domains_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        tls_relation = testing.Relation(
            endpoint="tls-certificates-acme",
            interface="tls-certificates",
        )
        state_in = testing.State(
            config={
                "acme_ca_common_name": "domain.com",
                "acme_allowed_domains": "not a comma separated list",
            },
            relations=[tls_relation],
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "Config value for acme_allowed_domains is not valid, it must be a comma separated list"
        )

    def test_given_acme_tls_relation_and_bad_sans_dns_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        tls_relation = testing.Relation(
            endpoint="tls-certificates-acme",
            interface="tls-certificates",
        )
        state_in = testing.State(
            config={
                "acme_ca_common_name": "domain.com",
                "acme_ca_sans_dns": "not a comma separated list",
            },
            relations=[tls_relation],
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "Config value for acme_ca_sans_dns is not valid, it must be a comma separated list"
        )

    def test_given_access_tls_relation_and_bad_sans_dns_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        tls_relation = testing.Relation(
            endpoint="tls-certificates-access",
            interface="tls-certificates",
        )
        state_in = testing.State(
            config={"access_sans_dns": "not a comma separated list"},
            relations=[tls_relation],
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "Config value for access_sans_dns is not valid, it must be a comma separated list"
        )

    def test_given_access_tls_relation_and_bad_sans_ip_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        tls_relation = testing.Relation(
            endpoint="tls-certificates-access",
            interface="tls-certificates",
        )
        state_in = testing.State(
            config={"access_sans_ip": "not an ip, also bad"},
            relations=[tls_relation],
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "Config value for access_sans_ip is not valid, it must be a comma separated list of IP addresses"
        )

    def test_given_peer_relation_not_created_when_collect_unit_status_then_status_is_waiting(self):
        state_in = testing.State(
            relations=[],
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == WaitingStatus("Waiting for peer relation")

    def test_given_bind_address_not_available_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
        )
        state_in = testing.State(
            relations=[peer_relation],
            networks={
                testing.Network(
                    "openbao-peers",
                    bind_addresses=[testing.BindAddress([testing.Address("")])],
                )
            },
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == WaitingStatus("Waiting for bind address")

    def test_given_non_leader_and_unit_address_not_available_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
        )
        state_in = testing.State(
            relations=[peer_relation],
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == WaitingStatus(
            "Waiting for other units to provide their addresses"
        )

    def test_given_ca_certificate_not_available_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": False,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == WaitingStatus("Waiting for CA certificate in workload")

    def test_given_certificate_unavailable_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": False,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == WaitingStatus("Certificate is unavailable in the charm")

    def test_given_service_not_started_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(spec=Snap, revision="1.18/stable", services={})
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == WaitingStatus("Waiting for OpenBao service to start")

    def test_given_openbao_api_unavailable_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(
                spec=Snap, revision="1.18/stable", services={"server": {"active": True}}
            )
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": False,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == WaitingStatus("OpenBao API is not yet available")

    def test_given_openbao_uninitialized_and_seal_type_transit_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(
                spec=Snap, revision="1.18/stable", services={"server": {"active": True}}
            )
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": False,
                "is_seal_type_transit.return_value": True,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus("Please initialize OpenBao")

    def test_given_openbao_uninitialized_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(
                spec=Snap, revision="1.18/stable", services={"server": {"active": True}}
            )
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": False,
                "is_seal_type_transit.return_value": False,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "Please initialize OpenBao or integrate with an auto-unseal provider"
        )

    def test_given_openbao_sealed_and_needs_migration_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(
                spec=Snap, revision="1.18/stable", services={"server": {"active": True}}
            )
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "is_seal_type_transit.return_value": False,
                "is_sealed.return_value": True,
                "needs_migration.return_value": True,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus("Please migrate OpenBao")

    def test_given_openbao_sealed_and_doesnt_need_migration_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(
                spec=Snap, revision="1.18/stable", services={"server": {"active": True}}
            )
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "is_seal_type_transit.return_value": False,
                "is_sealed.return_value": True,
                "needs_migration.return_value": False,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus("Please unseal OpenBao")

    def test_given_openbao_sealed_with_transit_seal_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(
                spec=Snap, revision="1.18/stable", services={"server": {"active": True}}
            )
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "is_seal_type_transit.return_value": True,
                "is_sealed.return_value": True,
                "needs_migration.return_value": False,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == WaitingStatus("Waiting for transit auto-unseal")

    def test_given_openbao_client_error_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(
                spec=Snap, revision="1.18/stable", services={"server": {"active": True}}
            )
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "is_seal_type_transit.return_value": False,
                "is_sealed.side_effect": OpenBaoClientError(),
                "needs_migration.return_value": False,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == MaintenanceStatus(
            "Seal check failed, waiting for OpenBao to recover"
        )

    def test_given_openbao_unauthorized_when_collect_unit_status_then_status_is_blocked(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(
                spec=Snap, revision="1.18/stable", services={"server": {"active": True}}
            )
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "is_seal_type_transit.return_value": False,
                "is_sealed.return_value": False,
                "needs_migration.return_value": False,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "Please authorize charm (see `authorize-charm` action)"
        )

    def test_given_openbao_authorized_when_collect_unit_status_then_status_is_active(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(
                spec=Snap, revision="1.18/stable", services={"server": {"active": True}}
            )
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "is_seal_type_transit.return_value": False,
                "is_sealed.return_value": False,
                "needs_migration.return_value": False,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        approle_secret = testing.Secret(
            label="openbao-approle-auth-details",
            tracked_content={
                "role-id": "existing role id",
                "secret-id": "existing secret id",
            },
        )
        state_in = testing.State(
            relations=[peer_relation], planned_units=3, secrets=[approle_secret]
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == ActiveStatus()

    def test_given_hsm_secret_id_without_granted_secret_when_collect_unit_status_then_blocked(
        self,
    ):
        self.mock_autounseal_requires_get_details.return_value = None
        state_in = testing.State(
            config={"hsm-config-secret-id": "secret:cqgj49fmp25c7796r0pg"},
        )
        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "hsm-config secret is not accessible; grant it to the charm with `juju grant-secret`"
        )

    def test_given_hsm_secret_missing_fields_when_collect_unit_status_then_blocked(self):
        self.mock_autounseal_requires_get_details.return_value = None
        hsm_secret = testing.Secret(tracked_content={"pin": "1234"})
        state_in = testing.State(
            config={"hsm-config-secret-id": hsm_secret.id},
            secrets=[hsm_secret],
        )
        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "hsm-config secret is missing required fields: slot or token-label, key-label or key-id"
        )

    def test_given_hsm_secret_without_lib_when_collect_unit_status_then_blocked(
        self, tmp_path: Path
    ):
        self.mock_autounseal_requires_get_details.return_value = None
        placeholder = tmp_path / "placeholder.so"
        placeholder.write_text("placeholder")
        hsm_secret = testing.Secret(
            tracked_content={
                "slot": "0",
                "pin": "1234",
                "key-label": "bao-root-key",
            },
        )
        state_in = testing.State(
            config={"hsm-config-secret-id": hsm_secret.id},
            secrets=[hsm_secret],
            resources=[testing.Resource(name="hsm-lib", path=placeholder)],
        )
        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "hsm-lib resource is not attached; use `juju attach-resource openbao hsm-lib=./some-lib.so`"
        )

    def test_given_hsm_ready_but_kms_plugin_missing_when_collect_unit_status_then_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self.mock_autounseal_requires_get_details.return_value = None
        missing_dir = tmp_path / "missing-plugins"
        monkeypatch.setattr("charm.PKCS11_KMS_PLUGIN_DIR", str(missing_dir))
        monkeypatch.setattr(
            "charm.PKCS11_KMS_PLUGIN_PATH", str(missing_dir / "openbao-plugin-kms-pkcs11")
        )
        monkeypatch.setattr(
            "charm.PKCS11_KMS_PLUGIN_VERSION_PATH", str(missing_dir / "pkcs11.version")
        )
        hsm_lib = tmp_path / "libykcs11.so"
        hsm_lib.write_bytes(b"\x7fELF" + b"\x00" * 16)
        hsm_secret = testing.Secret(
            tracked_content={
                "slot": "0",
                "pin": "1234",
                "key-label": "bao-root-key",
            },
        )
        state_in = testing.State(
            config={"hsm-config-secret-id": hsm_secret.id},
            secrets=[hsm_secret],
            resources=[testing.Resource(name="hsm-lib", path=hsm_lib)],
        )
        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "OpenBao snap does not include the PKCS#11 KMS plugin "
            "(requires an amd64/arm64 snap revision that ships it)"
        )

    def test_given_hsm_and_transit_autounseal_when_collect_unit_status_then_blocked(self):
        self.mock_autounseal_requires_get_details.return_value = AutounsealDetails(
            "1.2.3.4", "charm-autounseal", "key name", "role id", "secret id", "ca cert"
        )
        hsm_secret = testing.Secret(
            tracked_content={
                "slot": "0",
                "pin": "1234",
                "key-label": "bao-root-key",
            },
        )
        state_in = testing.State(
            config={"hsm-config-secret-id": hsm_secret.id},
            secrets=[hsm_secret],
        )
        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == BlockedStatus(
            "PKCS#11 HSM seal cannot be used together with transit auto-unseal"
        )

    def test_given_openbao_sealed_with_pkcs11_seal_when_collect_unit_status_then_status_is_waiting(
        self,
    ):
        self.mock_snap_cache.return_value = {
            "openbao": MagicMock(
                spec=Snap, revision="1.18/stable", services={"server": {"active": True}}
            )
        }
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            },
        )
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "is_seal_type_transit.return_value": False,
                "get_seal_type.return_value": "pkcs11",
                "is_sealed.return_value": True,
                "needs_migration.return_value": False,
            },
        )
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "1.2.3.4"},
                2: {"node_api_address": "1.2.3.5"},
                3: {"node_api_address": "1.2.3.6"},
            },
        )
        state_in = testing.State(
            relations=[peer_relation],
            planned_units=3,
        )

        state_out = self.ctx.run(self.ctx.on.collect_unit_status(), state_in)

        assert state_out.unit_status == WaitingStatus("Waiting for PKCS#11 auto-unseal")
