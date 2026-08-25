#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.


from openbao.openbao_helpers import (
    allowed_domains_config_is_valid,
    config_file_content_matches,
    hsm_config_secret_validation_error,
    pkcs11_seal_config_from_secret,
    sans_dns_config_is_valid,
    seal_type_has_changed,
)


def read_file(path: str) -> str:
    """Read a file and returns as a string."""
    with open(path, "r") as f:
        content = f.read()
    return content


class TestJujuConfigValidity:
    def test_given_one_domain_when_sans_dns_config_is_valid_returns_true(self):
        assert sans_dns_config_is_valid("example.com")

    def test_given_multiple_domains_when_sans_dns_config_is_valid_returns_true(self):
        assert sans_dns_config_is_valid("example.com,example.org")

    def test_given_empty_when_sans_dns_config_is_valid_returns_true(self):
        assert sans_dns_config_is_valid("")

    def test_given_invalid_string_when_sans_dns_config_is_valid_returns_false(self):
        assert not sans_dns_config_is_valid("This should have been a comma separated list")

    def test_given_valid_string_when_allowed_domains_config_is_valid_returns_true(self):
        assert allowed_domains_config_is_valid("example.com,example.org")

    def test_given_empty_when_allowed_domains_config_is_valid_returns_true(self):
        assert allowed_domains_config_is_valid("")

    def test_given_invalid_string_when_allowed_domains_config_is_valid_returns_false(self):
        assert not allowed_domains_config_is_valid("This should have been a comma separated list")


class TestSealTypeHasChanged:
    def test_given_identical_openbao_config_when_seal_type_has_changed_returns_false(self):
        existing_content = read_file("tests/unit/config.hcl")
        new_content = read_file("tests/unit/config.hcl")
        assert not seal_type_has_changed(existing_content, new_content)

    def test_given_different_seal_type_config_when_seal_type_has_changed_returns_true(self):
        existing_content = read_file("tests/unit/config.hcl")
        new_content = read_file("tests/unit/config_with_transit_stanza.hcl")
        assert seal_type_has_changed(existing_content, new_content)

    def test_given_pkcs11_seal_config_when_seal_type_has_changed_returns_true(self):
        existing_content = read_file("tests/unit/config.hcl")
        new_content = read_file("tests/unit/config_with_pkcs11_stanza.hcl")
        assert seal_type_has_changed(existing_content, new_content)

    def test_given_transit_to_pkcs11_when_seal_type_has_changed_returns_true(self):
        existing_content = read_file("tests/unit/config_with_transit_stanza.hcl")
        new_content = read_file("tests/unit/config_with_pkcs11_stanza.hcl")
        assert seal_type_has_changed(existing_content, new_content)


class TestConfigFileContentMatches:
    def test_given_identical_openbao_config_when_config_file_content_matches_returns_true(self):
        existing_content = read_file("tests/unit/config.hcl")
        new_content = read_file("tests/unit/config.hcl")

        matches = config_file_content_matches(
            existing_content=existing_content, new_content=new_content
        )

        assert matches

    def test_given_different_openbao_config_when_config_file_content_matches_returns_false(self):
        existing_content = read_file("tests/unit/config.hcl")
        new_content = read_file("tests/unit/config_with_raft_peers.hcl")

        matches = config_file_content_matches(
            existing_content=existing_content, new_content=new_content
        )

        assert not matches

    def test_given_equivalent_openbao_config_when_config_file_content_matches_returns_true(self):
        existing_content = read_file("tests/unit/config_with_raft_peers.hcl")
        new_content = read_file("tests/unit/config_with_raft_peers_equivalent.hcl")

        matches = config_file_content_matches(
            existing_content=existing_content, new_content=new_content
        )

        assert matches


class TestHsmConfigSecret:
    def test_given_complete_secret_when_validated_then_no_error(self):
        content = {
            "pin": "1234",
            "slot": "0",
            "token-label": "OpenBao",
            "key-label": "bao-root-key",
            "key-id": "0x01",
        }
        assert hsm_config_secret_validation_error(content) is None

    def test_given_minimal_slot_and_key_label_when_validated_then_no_error(self):
        content = {"pin": "1234", "slot": "0", "key-label": "bao-root-key"}
        assert hsm_config_secret_validation_error(content) is None

    def test_given_token_label_and_key_id_when_validated_then_no_error(self):
        content = {"pin": "1234", "token-label": "OpenBao", "key-id": "0x01"}
        assert hsm_config_secret_validation_error(content) is None

    def test_given_missing_pin_when_validated_then_error(self):
        content = {"slot": "0", "key-label": "bao-root-key"}
        error = hsm_config_secret_validation_error(content)
        assert error is not None
        assert "pin" in error

    def test_given_missing_slot_and_token_label_when_validated_then_error(self):
        content = {"pin": "1234", "key-label": "bao-root-key"}
        error = hsm_config_secret_validation_error(content)
        assert error is not None
        assert "slot or token-label" in error

    def test_given_missing_key_when_validated_then_error(self):
        content = {"pin": "1234", "slot": "0"}
        error = hsm_config_secret_validation_error(content)
        assert error is not None
        assert "key-label or key-id" in error

    def test_given_valid_secret_when_pkcs11_config_from_secret_then_fields_are_set(self):
        content = {
            "pin": "1234",
            "slot": "0",
            "token-label": "OpenBao",
            "key-label": "bao-root-key",
            "key-id": "0x01",
        }
        config = pkcs11_seal_config_from_secret(content, "/var/snap/openbao/common/hsm/pkcs11.so")
        assert config is not None
        assert config.lib == "/var/snap/openbao/common/hsm/pkcs11.so"
        assert config.pin == "1234"
        assert config.slot == "0"
        assert config.token_label == "OpenBao"
        assert config.key_label == "bao-root-key"
        assert config.key_id == "0x01"

    def test_given_empty_lib_when_pkcs11_config_from_secret_then_none(self):
        content = {"pin": "1234", "slot": "0", "key-label": "bao-root-key"}
        assert pkcs11_seal_config_from_secret(content, "") is None
