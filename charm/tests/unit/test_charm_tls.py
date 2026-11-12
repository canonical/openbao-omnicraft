#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests of the certificates OpenBao serves its API with, with the real TLSManager."""

from io import StringIO
from unittest.mock import patch

import ops.testing as testing
import pytest
from charmlibs.interfaces.tls_certificates import Certificate

from certificates import generate_example_provider_certificate
from fixtures import OpenBaoCharmFixtures


class TestCharmTLS(OpenBaoCharmFixtures):
    mock_tls_class = False

    @pytest.fixture(autouse=True)
    def workload_files(self, setup: None):
        """Back the pull and push of the workload mock with a dict of files."""
        self.files: dict[str, str] = {}
        self.mock_workload.push.side_effect = lambda path, source: self.files.update(
            {path: source}
        )
        self.mock_workload.pull.side_effect = self.pull
        self.mock_workload.exists.side_effect = lambda path: path in self.files
        self.tls_dir = self.mock_workload.tls_directory_path

    def pull(self, path: str) -> StringIO:
        if path not in self.files:
            raise FileNotFoundError(path)
        return StringIO(self.files[path])

    def state(self, *relations: testing.RelationBase, **kwargs: object) -> testing.State:
        return self.ready_state(*relations, storages=[testing.Storage("certs")], **kwargs)

    def test_given_leader_when_config_changed_then_self_signed_certificates_are_pushed(self):
        state_out = self.ctx.run(self.ctx.on.config_changed(), self.state())

        ca_secret = state_out.get_secret(label="self-signed-openbao-ca-certificate")
        ca = self.files[f"{self.tls_dir}/ca.pem"]
        assert ca == ca_secret.tracked_content["certificate"]
        certificate = Certificate.from_string(self.files[f"{self.tls_dir}/cert.pem"])
        assert certificate.common_name == "1.2.1.2"
        assert "myhostname" in (certificate.sans_dns or set())
        assert "1.2.1.2" in (certificate.sans_ip or set())
        assert f"{self.tls_dir}/key.pem" in self.files
        self.mock_workload.restart.assert_called()

    def test_given_valid_self_signed_certificate_when_config_changed_then_it_is_kept(self):
        state_out = self.ctx.run(self.ctx.on.config_changed(), self.state())
        certificate = self.files[f"{self.tls_dir}/cert.pem"]
        self.mock_workload.restart.reset_mock()

        self.ctx.run(self.ctx.on.config_changed(), self.state(secrets=list(state_out.secrets)))

        assert self.files[f"{self.tls_dir}/cert.pem"] == certificate
        self.mock_workload.restart.assert_not_called()

    def test_given_access_sans_changed_when_config_changed_then_certificate_is_reissued(self):
        state_out = self.ctx.run(self.ctx.on.config_changed(), self.state())
        certificate = self.files[f"{self.tls_dir}/cert.pem"]

        self.ctx.run(
            self.ctx.on.config_changed(),
            self.state(secrets=list(state_out.secrets), config={"access_sans_dns": "bao.com"}),
        )

        reissued = self.files[f"{self.tls_dir}/cert.pem"]
        assert reissued != certificate
        assert "bao.com" in (Certificate.from_string(reissued).sans_dns or set())

    def test_given_non_leader_without_ca_secret_when_config_changed_then_nothing_is_pushed(self):
        self.ctx.run(self.ctx.on.config_changed(), self.state(leader=False))

        assert self.files == {}

    def test_given_no_bind_address_when_config_changed_then_no_certificate_is_issued(self):
        # A certificate without the unit's address would be replaced as soon
        # as the address is known, restarting OpenBao.
        unbound = testing.Network("openbao-peers", bind_addresses=[], ingress_addresses=[])

        state_out = self.ctx.run(self.ctx.on.config_changed(), self.state(networks=[unbound]))

        assert state_out.get_secret(label="self-signed-openbao-ca-certificate")
        assert self.files == {}

    def test_given_ca_secret_when_peer_relation_changed_then_non_leader_issues_certificate(self):
        leader_out = self.ctx.run(self.ctx.on.config_changed(), self.state())
        self.files.clear()
        peer_relation = testing.PeerRelation(endpoint="openbao-peers", peers_data={1: {}})

        self.ctx.run(
            self.ctx.on.relation_changed(peer_relation, remote_unit=1),
            self.state(leader=False, relations=[peer_relation], secrets=list(leader_out.secrets)),
        )

        assert f"{self.tls_dir}/cert.pem" in self.files

    def test_given_access_relation_certificate_when_relation_changed_then_it_is_pushed(self):
        relation = testing.Relation(endpoint="tls-certificates-access")
        provider_certificate, private_key = generate_example_provider_certificate(
            "1.2.1.2", relation_id=relation.id
        )
        with patch(
            "managers.tls.TLSCertificatesRequiresV4.get_assigned_certificate",
            return_value=(provider_certificate, private_key),
        ):
            self.ctx.run(self.ctx.on.relation_changed(relation), self.state(relation))

        assert self.files[f"{self.tls_dir}/cert.pem"] == str(provider_certificate.certificate)
        assert self.files[f"{self.tls_dir}/key.pem"] == str(private_key)
        assert self.files[f"{self.tls_dir}/ca.pem"] == str(provider_certificate.ca)
        self.mock_workload.restart.assert_called()

    def test_given_ca_in_workload_when_send_ca_cert_joined_then_ca_is_sent(self):
        self.files[f"{self.tls_dir}/ca.pem"] = "my ca"
        relation = testing.Relation(endpoint="send-ca-cert")
        with patch("managers.tls.CertificateTransferProvides.add_certificates") as add:
            self.ctx.run(self.ctx.on.relation_joined(relation), self.state(relation))

        add.assert_called_once_with(certificates={"my ca"}, relation_id=relation.id)

    def test_given_workload_not_accessible_when_update_status_then_ca_is_left_alone(self):
        self.mock_workload.is_accessible.return_value = False
        self.mock_workload.pull.side_effect = ConnectionError("Could not connect to Pebble")
        relation = testing.Relation(endpoint="send-ca-cert")
        with (
            patch("managers.tls.CertificateTransferProvides.add_certificates") as add,
            patch("managers.tls.CertificateTransferProvides.remove_all_certificates") as remove,
        ):
            self.ctx.run(self.ctx.on.update_status(), self.state(relation))

        add.assert_not_called()
        remove.assert_not_called()
