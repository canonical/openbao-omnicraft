#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.


from openbao.openbao_helpers import (
    Pkcs11SealConfiguration,
    allowed_domains_config_is_valid,
    config_file_content_matches,
    hsm_config_secret_validation_error,
    is_hsm_lib_archive,
    is_hsm_lib_resource_usable,
    pkcs11_seal_config_from_secret,
    render_openbao_config_file,
    resolve_hsm_pkcs11_module,
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

    def test_given_plugin_metadata_when_pkcs11_config_from_secret_then_fields_are_set(self):
        content = {"pin": "1234", "slot": "0", "key-label": "bao-root-key"}
        config = pkcs11_seal_config_from_secret(
            content,
            "/var/snap/openbao/common/hsm/pkcs11.so",
            plugin_directory="/snap/openbao/current/plugins",
            plugin_command="openbao-plugin-kms-pkcs11",
            plugin_version="v0.1.0",
            plugin_sha256sum="abc123",
        )
        assert config is not None
        assert config.plugin_directory == "/snap/openbao/current/plugins"
        assert config.plugin_command == "openbao-plugin-kms-pkcs11"
        assert config.plugin_version == "v0.1.0"
        assert config.plugin_sha256sum == "abc123"

    def test_given_pkcs11_plugin_config_when_render_then_plugin_and_seal_present(self, tmp_path):
        template = tmp_path / "openbao.hcl.j2"
        template.write_text(
            "{% if pkcs11_lib %}\n"
            "{% if pkcs11_plugin_directory %}\n"
            'plugin_directory = "{{ pkcs11_plugin_directory }}"\n'
            "plugin_auto_register = true\n"
            "\n"
            'plugin "kms" "pkcs11" {\n'
            '  command   = "{{ pkcs11_plugin_command }}"\n'
            '  version   = "{{ pkcs11_plugin_version }}"\n'
            '  sha256sum = "{{ pkcs11_plugin_sha256sum }}"\n'
            "}\n"
            "{% endif %}\n"
            'seal "pkcs11" {\n'
            '  lib = "{{ pkcs11_lib }}"\n'
            '  pin = "{{ pkcs11_pin }}"\n'
            "}\n"
            "{% endif %}\n"
        )
        rendered = render_openbao_config_file(
            config_template_path=str(tmp_path),
            config_template_name="openbao.hcl.j2",
            default_lease_ttl="168h",
            max_lease_ttl="720h",
            cluster_address="https://1.2.3.4:8201",
            api_address="https://1.2.3.4:8200",
            tls_cert_file="/certs/cert.pem",
            tls_key_file="/certs/key.pem",
            tcp_address="[::]:8200",
            raft_storage_path="/raft",
            node_id="unit-0",
            retry_joins=[],
            log_level="info",
            pkcs11_config=Pkcs11SealConfiguration(
                lib="/var/snap/openbao/common/hsm/pkcs11.so",
                pin="1234",
                plugin_directory="/snap/openbao/current/plugins",
                plugin_command="openbao-plugin-kms-pkcs11",
                plugin_version="v0.1.0",
                plugin_sha256sum="deadbeef",
            ),
        )
        assert 'plugin_directory = "/snap/openbao/current/plugins"' in rendered
        assert "plugin_auto_register = true" in rendered
        assert 'plugin "kms" "pkcs11"' in rendered
        assert 'command   = "openbao-plugin-kms-pkcs11"' in rendered
        assert 'version   = "v0.1.0"' in rendered
        assert 'sha256sum = "deadbeef"' in rendered
        assert 'seal "pkcs11"' in rendered


class TestHsmLibResourceHelpers:
    def test_given_placeholder_when_resource_usable_then_false(self, tmp_path):
        placeholder = tmp_path / "placeholder"
        placeholder.write_text("placeholder")
        assert not is_hsm_lib_resource_usable(placeholder)

    def test_given_elf_when_resource_usable_then_true(self, tmp_path):
        lib = tmp_path / "lib.so"
        lib.write_bytes(b"\x7fELF" + b"\x00" * 16)
        assert is_hsm_lib_resource_usable(lib)

    def test_given_tarball_when_archive_detected(self, tmp_path):
        import tarfile

        member = tmp_path / "pkcs11.so"
        member.write_bytes(b"\x7fELF" + b"\x00" * 16)
        archive = tmp_path / "hsm-lib.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(member, arcname="pkcs11.so")
        assert is_hsm_lib_archive(archive)
        assert is_hsm_lib_resource_usable(archive)

    def test_given_pkcs11_so_when_resolve_then_preferred(self, tmp_path):
        (tmp_path / "dep.so").write_bytes(b"\x7fELF" + b"\x00" * 8)
        module = tmp_path / "pkcs11.so"
        module.write_bytes(b"\x7fELF" + b"\x00" * 8)
        assert resolve_hsm_pkcs11_module(tmp_path) == module.resolve()

    def test_given_lib_secret_when_resolve_then_uses_relative_path(self, tmp_path):
        (tmp_path / "pkcs11.so").write_bytes(b"\x7fELF" + b"\x00" * 8)
        named = tmp_path / "yubihsm_pkcs11.so"
        named.write_bytes(b"\x7fELF" + b"\x00" * 8)
        assert resolve_hsm_pkcs11_module(tmp_path, "yubihsm_pkcs11.so") == named.resolve()

    def test_given_path_escape_when_resolve_then_none(self, tmp_path):
        assert resolve_hsm_pkcs11_module(tmp_path, "../etc/passwd") is None

    def test_given_multiple_sos_without_lib_when_resolve_then_none(self, tmp_path):
        (tmp_path / "a.so").write_bytes(b"\x7fELF" + b"\x00" * 8)
        (tmp_path / "b.so").write_bytes(b"\x7fELF" + b"\x00" * 8)
        assert resolve_hsm_pkcs11_module(tmp_path) is None
