#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests of the feature managers, with Juju and OpenBao mocked."""

import json
from datetime import timedelta
from unittest.mock import MagicMock, call, patch

import pytest
from charmlibs.interfaces.tls_certificates import (
    CertificateRequestAttributes,
    CertificateRequestErrorCode,
    generate_ca,
    generate_private_key,
)

from certificates import generate_example_provider_certificate, generate_example_requirer_csr
from common.exceptions import ManagerError
from common.juju_facade import NoSuchSecretError
from common.openbao_autounseal import AutounsealDetails
from common.openbao_client import (
    AppRole,
    OpenBaoAuthenticationError,
    OpenBaoClientError,
    PKICertificateError,
    SecretsBackend,
    Token,
)
from common.openbao_s3 import S3Error
from managers.acme import ACMEManager
from managers.autounseal import AutounsealProviderManager, AutounsealRequirerManager
from managers.backup import BackupManager
from managers.kv import KVManager
from managers.pki import PKIManager, _map_openbao_pki_errors
from managers.raft import RaftManager

S3_PARAMETERS = {
    "bucket": " my-bucket ",
    "access-key": "access",
    "secret-key": "secret",
    "endpoint": "http://s3",
}


def juju_facade(module: str, **attributes: object) -> MagicMock:
    """Patch the JujuFacade of a manager module and return the mock of its instance."""
    facade = MagicMock(is_leader=True)
    facade.configure_mock(**attributes)
    patcher = patch(f"managers.{module}.JujuFacade", return_value=facade)
    patcher.start()
    return facade


@pytest.fixture(autouse=True)
def stop_patches():
    yield
    patch.stopall()


class TestBackupManager:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.juju = juju_facade("backup")
        self.s3_requirer = MagicMock()
        self.s3_requirer.get_s3_connection_info.return_value = dict(S3_PARAMETERS)
        self.s3 = MagicMock()
        self.s3_class = patch("managers.backup.S3", return_value=self.s3).start()
        self.openbao = MagicMock()
        charm = MagicMock()
        charm.model.name = "model"
        self.manager = BackupManager(charm, self.s3_requirer, "s3-parameters")

    @pytest.mark.parametrize("operation", ["create", "list", "restore"])
    @pytest.mark.parametrize(
        "is_leader, relation_exists, s3_parameters, message",
        [
            (False, True, S3_PARAMETERS, "Only leader unit can perform backup operations"),
            (True, False, S3_PARAMETERS, "S3 relation not created"),
            (
                True,
                True,
                {"bucket": "b"},
                "S3 parameters missing (access-key, secret-key, endpoint)",
            ),
        ],
    )
    def test_given_prerequisites_not_met_when_backup_operation_then_error_is_raised(
        self,
        operation: str,
        is_leader: bool,
        relation_exists: bool,
        s3_parameters: dict,
        message: str,
    ):
        self.juju.is_leader = is_leader
        self.juju.relation_exists.return_value = relation_exists
        self.s3_requirer.get_s3_connection_info.return_value = dict(s3_parameters)
        run = {
            "create": lambda: self.manager.create_backup(self.openbao),
            "list": lambda: self.manager.list_backups(),
            "restore": lambda: self.manager.restore_backup(self.openbao, "key"),
        }[operation]

        with pytest.raises(ManagerError, match=message.replace("(", r"\(").replace(")", r"\)")):
            run()

    def test_when_create_backup_then_snapshot_is_uploaded_to_the_trimmed_bucket(self):
        backup_key = self.manager.create_backup(self.openbao, skip_verify=True)

        assert backup_key.startswith("openbao-backup-model-")
        assert self.s3_class.call_args.kwargs["skip_verify"] is True
        self.s3.create_bucket.assert_called_once_with(bucket_name="my-bucket")
        self.s3.upload_content.assert_called_once_with(
            content=self.openbao.create_snapshot.return_value.raw,
            bucket_name="my-bucket",
            key=backup_key,
        )

    @pytest.mark.parametrize(
        "failure, message",
        [
            ("session", "Failed to create S3 session"),
            ("bucket", "Failed to create S3 bucket"),
            ("upload", "Failed to upload backup to S3 bucket"),
        ],
    )
    def test_given_s3_failure_when_create_backup_then_error_is_raised(
        self, failure: str, message: str
    ):
        if failure == "session":
            self.s3_class.side_effect = S3Error("bad endpoint")
        elif failure == "bucket":
            self.s3.create_bucket.return_value = False
        else:
            self.s3.upload_content.return_value = False

        with pytest.raises(ManagerError, match=message):
            self.manager.create_backup(self.openbao)

    def test_when_list_backups_then_keys_with_the_backup_prefix_are_returned(self):
        self.s3.get_object_key_list.return_value = ["openbao-backup-1"]

        assert self.manager.list_backups() == ["openbao-backup-1"]
        self.s3.get_object_key_list.assert_called_once_with(
            bucket_name="my-bucket", prefix="openbao-backup-"
        )

    def test_given_s3_error_when_list_backups_then_error_is_raised(self):
        self.s3.get_object_key_list.side_effect = S3Error("denied")

        with pytest.raises(ManagerError, match="Failed to list backups in S3 bucket: denied"):
            self.manager.list_backups()

    def test_when_restore_backup_then_snapshot_is_restored(self):
        self.manager.restore_backup(self.openbao, "key")

        self.s3.get_content.assert_called_once_with(bucket_name="my-bucket", object_key="key")
        self.openbao.restore_snapshot.assert_called_once_with(
            snapshot=self.s3.get_content.return_value
        )

    @pytest.mark.parametrize(
        "failure, message",
        [
            ("missing", r"Snapshot not found in S3 bucket. \(my-bucket/key\)"),
            ("s3", "Failed to retrieve snapshot from S3"),
            ("openbao", "Failed to restore snapshot"),
        ],
    )
    def test_given_failure_when_restore_backup_then_error_is_raised(
        self, failure: str, message: str
    ):
        if failure == "missing":
            self.s3.get_content.return_value = None
        elif failure == "s3":
            self.s3.get_content.side_effect = S3Error("denied")
        else:
            self.openbao.restore_snapshot.side_effect = OpenBaoClientError("sealed")

        with pytest.raises(ManagerError, match=message):
            self.manager.restore_backup(self.openbao, "key")


class TestRaftManager:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.juju = juju_facade("raft", planned_units_for_app=1)
        self.workload = MagicMock(raft_storage_path="/raft")
        self.manager = RaftManager(MagicMock(), self.workload)

    @pytest.mark.parametrize(
        "is_leader, planned_units, message",
        [
            (False, 1, "Only the leader unit can bootstrap a OpenBao cluster"),
            (True, 3, "Bootstrapping a OpenBao cluster requires exactly one unit"),
        ],
    )
    def test_given_not_single_leader_when_bootstrap_then_error_is_raised(
        self, is_leader: bool, planned_units: int, message: str
    ):
        self.juju.is_leader = is_leader
        self.juju.planned_units_for_app = planned_units

        with pytest.raises(ManagerError, match=message):
            self.manager.bootstrap("node", "https://1.2.3.4:8200")
        self.workload.stop.assert_not_called()

    def test_given_single_leader_when_bootstrap_then_peers_json_is_written_while_stopped(self):
        self.manager.bootstrap("node", "https://1.2.3.4:8200")

        assert self.workload.mock_calls == [
            call.stop(),
            call.push(
                "/raft/raft/peers.json",
                json.dumps([{"id": "node", "address": "https://1.2.3.4:8200"}]),
            ),
            call.restart(),
        ]


class TestKVManager:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.juju = juju_facade("kv")
        self.juju.set_app_secret_content.return_value.id = "secret:new"
        self.openbao = MagicMock()
        self.openbao.create_or_update_approle.return_value = "role id"
        self.openbao.generate_role_secret_id.return_value = "role secret id"
        self.kv = MagicMock()
        self.kv.get_credentials.return_value = {}
        self.relation = MagicMock()
        self.manager = KVManager(MagicMock(), self.openbao, self.kv, "ca")

    def generate(self) -> None:
        self.manager.generate_credentials_for_requirer(
            relation=self.relation,
            app_name="app",
            unit_name="app/0",
            mount_suffix="suffix",
            egress_subnets=["10.0.0.0/24"],
            nonce="nonce",
            openbao_url="https://1.2.3.4:8200",
        )

    def test_given_new_unit_when_generate_credentials_then_approle_is_created_and_shared(self):
        self.juju.get_latest_secret_content.side_effect = NoSuchSecretError()

        self.generate()

        self.openbao.enable_secrets_engine.assert_called_once_with(
            SecretsBackend.KV_V2, "charm-app-suffix"
        )
        self.openbao.create_or_update_approle.assert_called_once()
        assert self.openbao.create_or_update_approle.call_args.kwargs["cidrs"] == ["10.0.0.0/24"]
        self.juju.set_app_secret_content.assert_called_once_with(
            content={"role-id": "role id", "role-secret-id": "role secret id"},
            label="openbao-kv-app-0",
        )
        self.juju.grant_secret.assert_called_once()
        self.kv.set_kv_data.assert_called_once_with(
            relation=self.relation,
            mount="charm-app-suffix",
            ca_certificate="ca",
            vault_url="https://1.2.3.4:8200",
            nonce="nonce",
            credentials_juju_secret_id="secret:new",
        )
        # The nonce has no credentials recorded yet, so it is stale.
        self.kv.remove_unit_credentials.assert_called_once_with(self.relation, nonce="nonce")

    def test_given_role_already_has_the_egress_subnets_when_generate_credentials_then_it_is_reused(
        self,
    ):
        self.kv.get_credentials.return_value = {"nonce": "secret:existing"}
        self.juju.get_latest_secret_content.return_value = {"role-secret-id": "role secret id"}
        self.openbao.read_role_secret.return_value = {"cidr_list": ["10.0.0.0/24"]}

        self.generate()

        self.openbao.create_or_update_approle.assert_not_called()
        assert self.kv.set_kv_data.call_args.kwargs["credentials_juju_secret_id"] == (
            "secret:existing"
        )
        self.kv.remove_unit_credentials.assert_not_called()

    @pytest.mark.parametrize("cidr_list", [["192.168.0.0/24"], OpenBaoClientError()])
    def test_given_role_outdated_when_generate_credentials_then_it_is_recreated(
        self, cidr_list: list | Exception
    ):
        self.kv.get_credentials.return_value = {"nonce": "secret:existing"}
        self.juju.get_latest_secret_content.return_value = {"role-secret-id": "role secret id"}
        if isinstance(cidr_list, Exception):
            self.openbao.read_role_secret.side_effect = cidr_list
        else:
            self.openbao.read_role_secret.return_value = {"cidr_list": cidr_list}

        self.generate()

        self.openbao.create_or_update_approle.assert_called_once()

    def test_given_not_leader_when_generate_credentials_then_nothing_is_done(self):
        self.juju.is_leader = False

        self.generate()

        self.openbao.enable_secrets_engine.assert_not_called()
        self.kv.set_kv_data.assert_not_called()

    def test_when_remove_unit_credentials_then_unit_secret_is_removed(self):
        KVManager.remove_unit_credentials(self.juju, "app/0")

        self.juju.remove_secret.assert_called_once_with("openbao-kv-app-0")


class TestAutounsealProviderManager:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.juju = juju_facade("autounseal")
        self.openbao = MagicMock()
        self.provides = MagicMock(relation_name="openbao-autounseal-provides")
        self.manager = AutounsealProviderManager(
            MagicMock(), self.openbao, self.provides, "ca", "charm-autounseal"
        )

    def test_when_create_credentials_then_key_policy_and_approle_are_created_and_shared(self):
        self.openbao.create_or_update_approle.return_value = "role id"
        self.openbao.generate_role_secret_id.return_value = "secret id"
        relation = MagicMock(id=7)

        credentials = self.manager.create_credentials(relation, "https://1.2.3.4:8200")

        assert credentials == ("7", "role id", "secret id")
        self.openbao.create_transit_key.assert_called_once_with(
            mount_point="charm-autounseal", key_name="7"
        )
        policy_name, policy = self.openbao.create_or_update_policy.call_args.args
        assert policy_name == "charm-autounseal-7"
        assert 'path "charm-autounseal/encrypt/7"' in policy
        assert 'path "charm-autounseal/decrypt/7"' in policy
        self.provides.set_autounseal_data.assert_called_once_with(
            relation,
            "https://1.2.3.4:8200",
            "charm-autounseal",
            "7",
            "role id",
            "secret id",
            "ca",
        )

    def test_when_get_outstanding_requests_then_relations_without_valid_credentials_are_returned(
        self,
    ):
        no_secret, missing_secret, stale, valid = (MagicMock(id=i) for i in range(4))
        self.juju.get_active_relations.return_value = [no_secret, missing_secret, stale, valid]
        self.juju.get_app_relation_data.side_effect = lambda _, relation_id: (
            {} if relation_id == 0 else {"credentials_secret_id": f"secret:{relation_id}"}
        )

        def secret_values(*_: str, id: str) -> tuple[str, str]:
            if id == "secret:1":
                raise NoSuchSecretError()
            return ("role id", id)

        def read_role_secret(role: str, secret_id: str) -> dict:
            # Only the credentials of the last relation are still known to OpenBao.
            if secret_id != "secret:3":
                raise OpenBaoClientError()
            return {}

        self.juju.get_secret_content_values.side_effect = secret_values
        self.openbao.read_role_secret.side_effect = read_role_secret

        assert self.manager.get_outstanding_requests() == [no_secret, missing_secret, stale]

    def test_when_clean_up_credentials_then_unused_roles_and_policies_are_removed(self):
        self.juju.get_active_relations.return_value = [MagicMock(id=1)]
        self.openbao.list.side_effect = lambda path: {
            "auth/approle/role": ["charm-autounseal-1", "charm-autounseal-2", "charm"],
            "sys/policy": ["charm-autounseal-1", "charm-autounseal-2", "default"],
            "charm-autounseal/keys": ["1", "2"],
        }[path]
        self.openbao.read.return_value = {"deletion_allowed": False}

        self.manager.clean_up_credentials()

        self.openbao.delete_role.assert_called_once_with("charm-autounseal-2")
        self.openbao.delete_policy.assert_called_once_with("charm-autounseal-2")
        # Orphaned keys are never deleted by the charm, only allowed to be.
        self.openbao.write.assert_called_once_with(
            "charm-autounseal/keys/2/config", {"deletion_allowed": True}
        )


class TestAutounsealRequirerManager:
    DETAILS = AutounsealDetails("https://5.6.7.8:8200", "mount", "key", "role", "secret", "ca")

    @pytest.fixture(autouse=True)
    def setup(self):
        self.juju = juju_facade("autounseal")
        self.provider = MagicMock(token="new token")
        patch("managers.autounseal.OpenBaoClient", return_value=self.provider).start()
        self.manager = AutounsealRequirerManager(MagicMock(), MagicMock())

    def test_given_valid_stored_token_when_get_token_then_it_is_reused(self):
        self.juju.get_secret_content_values.return_value = ["stored token"]
        self.provider.authenticate.return_value = True
        self.provider.token = "stored token"

        assert self.manager.get_provider_openbao_token(self.DETAILS, "/ca.pem") == "stored token"
        self.provider.authenticate.assert_called_once_with(Token("stored token"))
        self.juju.set_app_secret_content.assert_not_called()

    @pytest.mark.parametrize("stored_token_rejected", [False, OpenBaoAuthenticationError()])
    def test_given_invalid_stored_token_when_get_token_then_leader_stores_a_new_one(
        self, stored_token_rejected: bool | Exception
    ):
        self.juju.get_secret_content_values.return_value = ["stored token"]
        self.provider.authenticate.side_effect = [stored_token_rejected, True]

        assert self.manager.get_provider_openbao_token(self.DETAILS, "/ca.pem") == "new token"
        self.provider.authenticate.assert_called_with(AppRole("role", "secret"))
        self.juju.set_app_secret_content.assert_called_once_with(
            {"token": "new token"}, label="openbao-autounseal-token"
        )


class TestPKIManager:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.juju = juju_facade("pki")
        self.openbao = MagicMock()
        self.openbao.get_intermediate_ca.return_value = None
        self.openbao.list_pki_issuers.return_value = ["issuer"]
        self.openbao_pki = MagicMock()
        ca_key = generate_private_key()
        self.ca = generate_ca(ca_key, timedelta(days=10), "pki.example.com")
        self.ca_key = str(ca_key)
        self.openbao.generate_self_signed_ca.return_value = (str(self.ca), self.ca_key)

    def manager(self, tls_certificates_pki: MagicMock | None = None) -> PKIManager:
        return PKIManager(
            charm=MagicMock(),
            openbao_client=self.openbao,
            certificate_request_attributes=CertificateRequestAttributes(
                common_name="pki.example.com", is_ca=True
            ),
            mount_point="charm-pki",
            role_name="charm-pki",
            allowed_domains=None,
            allow_bare_domains=None,
            allow_subdomains=None,
            allow_wildcard_certificates=None,
            allow_any_name=None,
            allow_ip_sans=None,
            organization=None,
            organizational_unit=None,
            country=None,
            province=None,
            locality=None,
            openbao_pki=self.openbao_pki,
            tls_certificates_pki=tls_certificates_pki,
        )

    def test_given_no_self_signed_ca_when_configure_then_one_is_generated_and_stored(self):
        self.juju.get_secret_content_values.side_effect = NoSuchSecretError()
        self.openbao.role_config_matches_given_config.return_value = False

        self.manager().configure()

        self.openbao.enable_secrets_engine.assert_called_once_with(SecretsBackend.PKI, "charm-pki")
        assert self.openbao.generate_self_signed_ca.call_args.kwargs["ttl"] == "87600h"
        self.juju.set_app_secret_content.assert_called_once_with(
            {"certificate": str(self.ca), "privatekey": self.ca_key},
            label="openbao-pki-self-signed-ca",
        )
        role = self.openbao.create_or_update_pki_charm_role.call_args.kwargs
        assert role["allowed_domains"] == "pki.example.com"
        # Issued certificates last half as long as the CA.
        assert role["max_ttl"] == f"{5 * 24 * 3600}s"

    def test_given_stored_ca_missing_from_openbao_when_configure_then_it_is_reimported(self):
        self.juju.get_secret_content_values.return_value = (str(self.ca), self.ca_key)

        self.manager().configure()

        self.openbao.generate_self_signed_ca.assert_not_called()
        self.openbao.import_ca_certificate_and_key.assert_called_once_with(
            certificate=str(self.ca), private_key=self.ca_key, mount="charm-pki"
        )

    def test_given_stored_ca_in_openbao_when_configure_then_it_is_kept(self):
        self.juju.get_secret_content_values.return_value = (str(self.ca), self.ca_key)
        self.openbao.get_intermediate_ca.return_value = str(self.ca)

        self.manager().configure()

        self.openbao.generate_self_signed_ca.assert_not_called()
        self.openbao.import_ca_certificate_and_key.assert_not_called()

    def test_given_external_ca_issued_when_configure_then_it_is_imported(self):
        provider_certificate, private_key = generate_example_provider_certificate(
            "pki.example.com", relation_id=1
        )
        tls_certificates_pki = MagicMock()
        tls_certificates_pki.get_assigned_certificate.return_value = (
            provider_certificate,
            private_key,
        )

        self.manager(tls_certificates_pki).configure()

        self.openbao.import_ca_certificate_and_key.assert_called_once_with(
            certificate=str(provider_certificate.certificate),
            private_key=str(private_key),
            mount="charm-pki",
        )
        self.openbao.generate_self_signed_ca.assert_not_called()

    def test_given_not_leader_when_configure_or_sync_then_nothing_is_done(self):
        self.juju.is_leader = False
        manager = self.manager()

        manager.configure()
        manager.sync()

        self.openbao.enable_secrets_engine.assert_not_called()
        self.openbao_pki.get_outstanding_certificate_requests.assert_not_called()

    def test_given_outstanding_request_when_sync_then_certificate_is_signed_and_sent(self):
        self.juju.get_secret_content_values.return_value = (str(self.ca), self.ca_key)
        request = generate_example_requirer_csr("app.pki.example.com", relation_id=3)
        self.openbao_pki.get_outstanding_certificate_requests.return_value = [request]
        self.openbao.sign_pki_certificate_signing_request.return_value = MagicMock(
            certificate=str(self.ca), ca=str(self.ca), chain=[str(self.ca)]
        )

        self.manager().sync()

        signed = self.openbao.sign_pki_certificate_signing_request.call_args.kwargs
        assert signed["common_name"] == "app.pki.example.com"
        assert signed["ttl"] == f"{5 * 24 * 3600}s"
        sent = self.openbao_pki.set_relation_certificate.call_args.kwargs["provider_certificate"]
        assert sent.relation_id == 3

    def test_given_role_rejects_request_when_sync_then_error_is_sent_to_the_requirer(self):
        self.juju.get_secret_content_values.return_value = (str(self.ca), self.ca_key)
        request = generate_example_requirer_csr("other.com", relation_id=3)
        self.openbao_pki.get_outstanding_certificate_requests.return_value = [request]
        self.openbao.sign_pki_certificate_signing_request.side_effect = PKICertificateError(
            "common name other.com not allowed by this role"
        )

        self.manager().sync()

        error = self.openbao_pki.set_relation_error.call_args.kwargs["provider_error"]
        assert error.error.code == CertificateRequestErrorCode.DOMAIN_NOT_ALLOWED
        self.openbao_pki.set_relation_certificate.assert_not_called()

    def test_given_role_not_created_when_sync_then_request_is_left_outstanding(self):
        self.openbao.is_pki_role_created.return_value = False
        self.openbao_pki.get_outstanding_certificate_requests.return_value = [
            generate_example_requirer_csr("app.pki.example.com", relation_id=3)
        ]

        self.manager().sync()

        self.openbao.sign_pki_certificate_signing_request.assert_not_called()

    @pytest.mark.parametrize(
        "message, code",
        [
            ("IP Subject Alternative Names are not allowed", "IP_NOT_ALLOWED"),
            ("subject alternative name x not allowed", "DOMAIN_NOT_ALLOWED"),
            ("common name example.org not allowed", "DOMAIN_NOT_ALLOWED"),
            ("something else went wrong", "OTHER"),
        ],
    )
    def test_when_map_openbao_pki_errors_then_certificate_error_code_is_set(
        self, message: str, code: str
    ):
        error = _map_openbao_pki_errors(PKICertificateError(message))

        assert error.code == CertificateRequestErrorCode[code]
        assert error.reason == message


class TestACMEManager:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.juju = juju_facade("acme")
        self.juju.relation_exists.return_value = True
        self.openbao = MagicMock()
        self.openbao.get_intermediate_ca.return_value = None
        self.openbao.list_pki_issuers.return_value = ["issuer"]
        self.tls_certificates_acme = MagicMock()
        self.manager = ACMEManager(
            charm=MagicMock(),
            openbao_client=self.openbao,
            mount_point="charm-acme",
            tls_certificates_acme=self.tls_certificates_acme,
            certificate_request_attributes=CertificateRequestAttributes(
                common_name="acme.example.com", is_ca=True
            ),
            role_name="charm-acme",
            openbao_address="https://1.2.3.4:8200",
            allowed_domains="example.com",
            allow_bare_domains=True,
            allow_subdomains=True,
            allow_wildcard_certificates=True,
            allow_any_name=False,
            allow_ip_sans=False,
            organization=None,
            organizational_unit=None,
            country=None,
            province=None,
            locality=None,
        )

    def test_given_intermediate_ca_issued_when_configure_then_acme_is_enabled(self):
        provider_certificate, private_key = generate_example_provider_certificate(
            "acme.example.com", relation_id=1
        )
        self.tls_certificates_acme.get_assigned_certificate.return_value = (
            provider_certificate,
            private_key,
        )

        self.manager.configure()

        self.openbao.import_ca_certificate_and_key.assert_called_once()
        self.openbao.set_urls.assert_called_once_with(
            issuing_certificates=["https://1.2.3.4:8200/v1/pki/ca"],
            crl_distribution_points=["https://1.2.3.4:8200/v1/pki/crl"],
            mount="charm-acme",
        )
        self.openbao.allow_acme_headers.assert_called_once_with(mount="charm-acme")

    def test_given_no_intermediate_ca_when_configure_then_acme_is_not_enabled(self):
        self.tls_certificates_acme.get_assigned_certificate.return_value = (None, None)

        self.manager.configure()

        self.openbao.import_ca_certificate_and_key.assert_not_called()
        self.openbao.set_urls.assert_not_called()
