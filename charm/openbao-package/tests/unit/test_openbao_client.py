#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

from contextlib import nullcontext as does_not_raise
from typing import ContextManager
from unittest.mock import MagicMock, patch

import pytest
import requests
from hvac.exceptions import Forbidden, InternalServerError, InvalidRequest

from openbao.openbao_client import (
    AppRole,
    AuditDeviceType,
    OpenBaoAuthenticationError,
    OpenBaoClient,
    OpenBaoClientError,
    SecretsBackend,
    Token,
)

TEST_PATH = "./tests/unit"


@patch("hvac.api.auth_methods.token.Token.lookup_self")
def test_given_token_as_auth_details_when_authenticate_then_token_is_set(_):
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    openbao.authenticate(Token("some token"))

    assert openbao._client.token == "some token"


@patch("hvac.api.auth_methods.token.Token.lookup_self")
def test_given_valid_token_as_auth_details_when_authenticate_then_authentication_succeeds(
    patch_lookup: MagicMock,
):
    patch_lookup.return_value = {"data": "random data"}
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    assert openbao.authenticate(Token("some token"))


@patch("hvac.api.auth_methods.token.Token.lookup_self")
def test_given_invalid_token_as_auth_details_when_authenticate_then_authentication_fails(
    patch_lookup: MagicMock,
):
    patch_lookup.side_effect = Forbidden()
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    with pytest.raises(OpenBaoAuthenticationError):
        openbao.authenticate(Token("some token"))


@patch("hvac.api.auth_methods.token.Token.lookup_self")
def test_given_transient_error_when_authenticate_then_returns_false(
    patch_lookup: MagicMock,
):
    patch_lookup.side_effect = requests.exceptions.ConnectionError()
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    assert not openbao.authenticate(Token("some token"))


@patch("hvac.api.auth_methods.token.Token.lookup_self")
@patch("hvac.api.auth_methods.approle.AppRole.login")
def test_given_approle_as_auth_details_when_authenticate_then_approle_login_is_called(
    patch_approle_login: MagicMock, _
):
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    openbao.authenticate(AppRole(role_id="some role id", secret_id="some secret id"))

    patch_approle_login.assert_called_with(
        role_id="some role id", secret_id="some secret id", use_token=True
    )


@patch("hvac.api.system_backend.health.Health.read_health_status")
def test_given_connection_error_when_is_api_available_then_return_false(
    patch_health_status: MagicMock,
):
    patch_health_status.side_effect = requests.exceptions.ConnectionError()
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")

    assert not openbao.is_api_available()


@patch("hvac.api.system_backend.health.Health.read_health_status")
def test_given_api_returns_when_is_api_available_then_return_true(patch_health_status: MagicMock):
    patch_health_status.return_value = requests.Response()
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")

    assert openbao.is_api_available()


@patch("hvac.api.system_backend.raft.Raft.read_raft_config")
def test_given_node_in_peer_list_when_is_node_in_raft_peers_then_returns_true(
    patch_health_status: MagicMock,
):
    node_id = "whatever node id"
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    patch_health_status.return_value = {"data": {"config": {"servers": [{"node_id": node_id}]}}}

    assert openbao.is_node_in_raft_peers(node_id)


@patch("hvac.api.system_backend.raft.Raft.read_raft_config")
def test_given_node_not_in_peer_list_when_is_node_in_raft_peers_then_returns_false(
    patch_health_status: MagicMock,
):
    node_id = "whatever node id"
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    patch_health_status.return_value = {
        "data": {"config": {"servers": [{"node_id": "not our node"}]}}
    }

    assert not openbao.is_node_in_raft_peers(node_id)


@patch("hvac.api.system_backend.raft.Raft.read_raft_config")
def test_given_1_node_in_raft_cluster_when_get_num_raft_peers_then_returns_1(
    patch_health_status: MagicMock,
):
    patch_health_status.return_value = {
        "data": {
            "config": {
                "servers": [
                    {"node_id": "node 1"},
                    {"node_id": "node 2"},
                    {"node_id": "node 3"},
                ]
            }
        }
    }

    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")

    openbao.get_num_raft_peers()

    assert openbao.get_num_raft_peers() == 3


@patch("hvac.api.system_backend.auth.Auth.enable_auth_method")
def test_given_approle_not_in_auth_methods_when_enable_approle_auth_then_approle_is_added_to_auth_methods(
    patch_enable_auth_method: MagicMock,
):
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")

    openbao.enable_approle_auth_method()

    patch_enable_auth_method.assert_called_once()


@patch("hvac.api.system_backend.audit.Audit.enable_audit_device")
def test_given_audit_device_is_not_yet_enabled_when_enable_audit_device_then_device_is_enabled(
    patch_enable_audit_device: MagicMock,
):
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    openbao.enable_audit_device(device_type=AuditDeviceType.FILE, path="stdout")
    patch_enable_audit_device.assert_called_once_with(
        device_type="file", options={"file_path": "stdout"}
    )


@patch("hvac.api.system_backend.audit.Audit.enable_audit_device")
def test_given_audit_device_is_enabled_when_enable_audit_device_then_nothing_happens(
    patch_enable_audit_device: MagicMock,
):
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    openbao.enable_audit_device(device_type=AuditDeviceType.FILE, path="stdout")
    patch_enable_audit_device.assert_called_once_with(
        device_type="file", options={"file_path": "stdout"}
    )


@patch("hvac.api.system_backend.policy.Policy.create_or_update_policy")
def test_given_policy_with_mount_when_configure_policy_then_policy_is_formatted_properly(
    patch_create_policy: MagicMock,
):
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    openbao.create_or_update_policy_from_file(
        "test-policy", path=f"{TEST_PATH}/kv_with_mount.hcl", mount="example"
    )
    with open(f"{TEST_PATH}/kv_mounted.hcl", "r") as f:
        policy = f.read()
        patch_create_policy.assert_called_with(
            name="test-policy",
            policy=policy,
        )


@patch("hvac.api.system_backend.policy.Policy.create_or_update_policy")
def test_given_policy_without_mount_when_configure_policy_then_policy_created_correctly(
    patch_create_policy: MagicMock,
):
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    openbao.create_or_update_policy_from_file("test-policy", path=f"{TEST_PATH}/kv_mounted.hcl")
    with open(f"{TEST_PATH}/kv_mounted.hcl", "r") as f:
        policy = f.read()
        patch_create_policy.assert_called_with(
            name="test-policy",
            policy=policy,
        )


@patch("hvac.api.auth_methods.approle.AppRole.read_role_id")
@patch("hvac.api.auth_methods.approle.AppRole.create_or_update_approle")
def test_given_approle_with_valid_params_when_configure_approle_then_approle_created(
    patch_create_approle: MagicMock, patch_read_role_id: MagicMock
):
    patch_read_role_id.return_value = {"data": {"role_id": "1234"}}
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    assert "1234" == openbao.create_or_update_approle(
        "test-approle",
        policies=["root", "default"],
        cidrs=["192.168.1.0/24"],
        token_max_ttl="1h",
        token_ttl="1h",
    )

    patch_create_approle.assert_called_with(
        "test-approle",
        bind_secret_id="true",
        token_ttl="1h",
        token_max_ttl="1h",
        token_policies=["root", "default"],
        token_bound_cidrs=["192.168.1.0/24"],
        token_period=None,
    )
    patch_read_role_id.assert_called_once()


@patch("hvac.api.system_backend.mount.Mount.enable_secrets_engine")
def test_given_secrets_engine_with_valid_params_when_enable_secrets_engine_then_secrets_engine_enabled(
    patch_enable_secrets_engine: MagicMock,
):
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    openbao.enable_secrets_engine(SecretsBackend.KV_V2, "some/path")

    patch_enable_secrets_engine.assert_called_with(
        backend_type=SecretsBackend.KV_V2.value,
        description=f"Charm created '{SecretsBackend.KV_V2.value}' backend",
        path="some/path",
    )


@patch("hvac.api.system_backend.mount.Mount.disable_secrets_engine")
def test_when_disable_secrets_engine_then_secrets_engine_disabled(
    mock_disable_secrets_engine: MagicMock,
):
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    openbao.disable_secrets_engine("some/path")

    mock_disable_secrets_engine.assert_called_with("some/path")


@patch("hvac.api.system_backend.health.Health.read_health_status")
def test_given_health_status_returns_200_when_is_active_then_return_true(
    patch_health_status: MagicMock,
):
    response = requests.Response()
    response.status_code = 200
    patch_health_status.return_value = response
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    assert openbao.is_active_or_standby()


@patch("hvac.api.system_backend.health.Health.read_health_status")
def test_given_health_status_returns_standby_when_is_active_then_return_false(
    patch_health_status: MagicMock,
):
    response = requests.Response()
    response.status_code = 429
    patch_health_status.return_value = response
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    assert openbao.is_active_or_standby()
    assert not openbao.is_active()


@patch("hvac.api.system_backend.health.Health.read_health_status")
def test_given_health_status_returns_5xx_when_is_active_then_return_false(
    patch_health_status: MagicMock,
):
    response = requests.Response()
    response.status_code = 501
    patch_health_status.return_value = response
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    assert not openbao.is_active_or_standby()


@patch("hvac.api.system_backend.health.Health.read_health_status")
def test_given_connection_error_when_is_active_then_return_false(patch_health_status: MagicMock):
    patch_health_status.side_effect = requests.exceptions.ConnectionError()
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    assert not openbao.is_active_or_standby()


@pytest.mark.parametrize(
    "exception_raised, expectation",
    [
        (InternalServerError(), does_not_raise()),
        (requests.exceptions.ConnectionError(), does_not_raise()),
        (ValueError(), pytest.raises(ValueError)),
        (TypeError(), pytest.raises(TypeError)),
    ],
)
def test_when_remove_raft_node_is_called_and_exception_raised_then_exception_is_surpressed_or_bubbled_up(
    exception_raised: Exception, expectation: ContextManager, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "hvac.api.system_backend.raft.Raft.remove_raft_node",
        MagicMock(side_effect=exception_raised),
    )
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    with expectation:
        openbao.remove_raft_node("node_id")


@pytest.mark.parametrize(
    "exception_raised, expectation",
    [
        (InternalServerError(), does_not_raise()),
        (requests.exceptions.ConnectionError(), does_not_raise()),
        (ValueError(), pytest.raises(ValueError)),
        (TypeError(), pytest.raises(TypeError)),
    ],
)
def test_when_is_node_in_raft_peers_called_and_exception_raised_then_exception_is_surpressed_or_bubbled_up(
    exception_raised: Exception, expectation: ContextManager, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "hvac.api.system_backend.raft.Raft.read_raft_config",
        MagicMock(side_effect=exception_raised),
    )
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    with expectation:
        openbao.is_node_in_raft_peers("node_id")


@pytest.mark.parametrize(
    "exception_raised, expectation",
    [
        (InternalServerError(), does_not_raise()),
        (requests.exceptions.ConnectionError(), does_not_raise()),
        (ValueError(), pytest.raises(ValueError)),
        (TypeError(), pytest.raises(TypeError)),
    ],
)
def test_when_get_num_raft_peers_called_andexception_raised_then_exception_is_surpressed_or_bubbled_up(
    exception_raised: Exception, expectation: ContextManager, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "hvac.api.system_backend.raft.Raft.read_raft_config",
        MagicMock(side_effect=exception_raised),
    )
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    with expectation:
        openbao.get_num_raft_peers()


@patch("hvac.Client.read")
def test_read(patch_read: MagicMock):
    patch_read.return_value = {"data": {"key": "value"}}
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    result = openbao.read("some/path")
    assert result == {"key": "value"}
    patch_read.assert_called_once_with("some/path")


@patch("hvac.Client.list")
def test_list(patch_list: MagicMock):
    patch_list.return_value = {"data": {"keys": ["key1", "key2"]}}
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    result = openbao.list("some/path")
    assert result == ["key1", "key2"]
    patch_list.assert_called_once_with("some/path")


@patch("hvac.api.secrets_engines.pki.Pki.read_role")
def test_given_role_config_matches_given_config_when_role_config_matches_given_config_then_returns_true(
    patch_read_role: MagicMock,
):
    patch_read_role.return_value = {
        "data": {
            "allowed_domains": ["example.com"],
            "allow_bare_domains": True,
            "allow_subdomains": True,
            "allow_wildcard_certificates": True,
            "allow_any_name": True,
            "allow_ip_sans": True,
            "organization": "test-organization",
            "organizational_unit": "test-organizational-unit",
            "country": "test-country",
            "province": "test-province",
            "locality": "test-locality",
        }
    }
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    assert openbao.role_config_matches_given_config(
        role="test-role",
        mount="some/path",
        allowed_domains=["example.com"],
        allow_bare_domains=True,
        allow_subdomains=True,
        allow_wildcard_certificates=True,
        allow_any_name=True,
        allow_ip_sans=True,
        organization="test-organization",
        organizational_unit="test-organizational-unit",
        country="test-country",
        province="test-province",
        locality="test-locality",
    )


@patch("hvac.api.secrets_engines.pki.Pki.read_role")
@pytest.mark.parametrize(
    "allowed_domains,allow_subdomains, allow_wildcard_certificates, allow_any_name, allow_ip_sans, organization, organizational_unit, country, province, locality",
    [
        (
            ["example.com"],
            True,
            True,
            False,
            True,
            "test-organization",
            "test-organizational-unit",
            "test-country",
            "test-province",
            "test-locality",
        ),
        (
            ["example.com"],
            True,
            False,
            False,
            True,
            "test-organization",
            "test-organizational-unit",
            "test-country",
            "test-province",
            "test-locality",
        ),
        (
            ["example.com"],
            False,
            True,
            False,
            True,
            "test-organization",
            "test-organizational-unit",
            "test-country",
            "test-province",
            "test-locality",
        ),
        (
            ["example.com"],
            False,
            False,
            True,
            True,
            "test-organization",
            "test-organizational-unit",
            "test-country",
            "test-province",
            "test-locality",
        ),
        (
            ["example.com"],
            False,
            False,
            False,
            True,
            "test-organization",
            "test-organizational-unit",
            "test-country",
            "test-province",
            "test-locality",
        ),
        (
            ["example.com", "example.org"],
            True,
            True,
            True,
            True,
            "test-organization",
            "test-organizational-unit",
            "test-country",
            "test-province",
            "test-locality",
        ),
        (
            ["example.com"],
            True,
            False,
            True,
            True,
            "test-organization",
            "test-organizational-unit",
            "test-country",
            "test-province",
            "test-locality",
        ),
        (
            ["example.com"],
            False,
            True,
            True,
            True,
            "test-organization",
            "test-organizational-unit",
            "test-country",
            "test-province",
            "test-locality",
        ),
        (
            ["example.com"],
            False,
            False,
            True,
            True,
            "test-organization",
            "test-organizational-unit",
            "test-country",
            "test-province",
            "test-locality",
        ),
        (
            ["example.com"],
            False,
            False,
            False,
            True,
            "test-organization",
            "test-organizational-unit",
            "test-country",
            "test-province",
            "test-locality",
        ),
        (
            ["example.com"],
            False,
            False,
            False,
            False,
            "test-organization",
            "test-organizational-unit",
            "test-country",
            "test-province",
            "test-locality",
        ),
        (["example.com"], False, False, False, False, "", "", "", "", ""),
        (["example.com"], False, False, False, False, "test-organization", "", "", "", ""),
        (["example.com"], False, False, False, False, "", "test-organizational-unit", "", "", ""),
        (["example.com"], False, False, False, False, "", "", "test-country", "", ""),
    ],
)
def test_given_role_config_does_not_match_given_config_when_role_config_matches_given_config_then_returns_false(
    patch_read_role: MagicMock,
    allowed_domains: list[str],
    allow_subdomains: bool,
    allow_wildcard_certificates: bool,
    allow_any_name: bool,
    allow_ip_sans: bool,
    organization: str,
    organizational_unit: str,
    country: str,
    province: str,
    locality: str,
):
    patch_read_role.return_value = {
        "data": {
            "allowed_domains": ["example.com"],
            "allow_subdomains": True,
            "allow_wildcard_certificates": True,
            "allow_any_name": True,
        }
    }
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")
    assert not openbao.role_config_matches_given_config(
        role="test-role",
        mount="some/path",
        allowed_domains=allowed_domains,
        allow_bare_domains=True,
        allow_subdomains=allow_subdomains,
        allow_wildcard_certificates=allow_wildcard_certificates,
        allow_any_name=allow_any_name,
        allow_ip_sans=allow_ip_sans,
        organization=organization,
        organizational_unit=organizational_unit,
        country=country,
        province=province,
        locality=locality,
    )


@patch("hvac.api.secrets_engines.pki.Pki.sign_certificate")
def test_when_sign_pki_csr_succeeds_then_certificate_object_returned(patch_sign: MagicMock):
    patch_sign.return_value = {
        "data": {
            "certificate": "cert-pem",
            "issuing_ca": "ca-pem",
            "ca_chain": ["chain-pem"],
        }
    }
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")

    certificate = openbao.sign_pki_certificate_signing_request(
        mount="pki",
        role="role",
        csr="csr-pem",
        common_name="example.com",
        ttl="720h",
    )

    assert certificate.certificate == "cert-pem"
    assert certificate.ca == "ca-pem"
    assert certificate.chain == ["chain-pem"]


@patch("hvac.api.secrets_engines.pki.Pki.generate_root")
def test_when_generate_self_signed_ca_then_returns_certificate_and_key(
    patch_generate_root: MagicMock,
):
    patch_generate_root.return_value = {
        "data": {
            "certificate": "cert-pem",
            "private_key": "key-pem",
        }
    }
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")

    cert, key = openbao.generate_self_signed_ca(
        mount="pki",
        common_name="my-ca",
        ttl="8760h",
    )

    assert cert == "cert-pem"
    assert key == "key-pem"
    patch_generate_root.assert_called_once_with(
        type="exported",
        common_name="my-ca",
        mount_point="pki",
        extra_params={"ttl": "8760h"},
    )


@patch("hvac.api.secrets_engines.pki.Pki.generate_root")
def test_when_generate_self_signed_ca_with_optional_params_then_extra_params_passed(
    patch_generate_root: MagicMock,
):
    patch_generate_root.return_value = {
        "data": {
            "certificate": "cert-pem",
            "private_key": "key-pem",
        }
    }
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")

    openbao.generate_self_signed_ca(
        mount="pki",
        common_name="my-ca",
        ttl="8760h",
        sans_dns=["example.com", "www.example.com"],
        country="US",
        province="CA",
        locality="San Francisco",
        organization="Test Org",
        organizational_unit="Test Unit",
    )

    patch_generate_root.assert_called_once_with(
        type="exported",
        common_name="my-ca",
        mount_point="pki",
        extra_params={
            "ttl": "8760h",
            "alt_names": "example.com,www.example.com",
            "country": "US",
            "province": "CA",
            "locality": "San Francisco",
            "organization": "Test Org",
            "organizational_unit": "Test Unit",
        },
    )


@patch("hvac.api.secrets_engines.pki.Pki.generate_root")
def test_when_generate_self_signed_ca_fails_then_openbao_client_error_raised(
    patch_generate_root: MagicMock,
):
    patch_generate_root.side_effect = InvalidRequest("some error")
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")

    with pytest.raises(OpenBaoClientError):
        openbao.generate_self_signed_ca(
            mount="pki",
            common_name="my-ca",
            ttl="8760h",
        )


@patch("hvac.api.secrets_engines.pki.Pki.sign_certificate")
def test_when_sign_pki_csr_denied_by_openbao_then_openbao_client_error_raised(
    patch_sign: MagicMock,
):
    patch_sign.side_effect = InvalidRequest()
    openbao = OpenBaoClient(url="http://whatever-url", ca_cert_path="whatever path")

    with pytest.raises(OpenBaoClientError):
        openbao.sign_pki_certificate_signing_request(
            mount="pki",
            role="role",
            csr="csr-pem",
            common_name="example.com",
            ttl="720h",
        )
