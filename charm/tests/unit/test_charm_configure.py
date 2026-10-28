#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import tarfile
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock

import hcl
import ops.testing as testing
import pytest

import charm
from certificates import generate_example_provider_certificate
from common.openbao_autounseal import AutounsealDetails
from common.openbao_client import AppRole
from fixtures import OpenBaoCharmFixtures, vm_only
from literals import Substrate
from workload_vm import OpenBaoVMWorkload

AUTOUNSEAL_DETAILS = AutounsealDetails(
    "1.2.3.4", "charm-autounseal", "key name", "role id", "secret id", "ca cert"
)
HSM_SECRET_CONTENT = {"slot": "0", "pin": "1234", "key-label": "bao-root-key"}
NOT_A_LIST = "This should have been a comma separated list"


class TestCharmConfigure(OpenBaoCharmFixtures):
    def configure(self, state: testing.State) -> testing.State:
        return self.ctx.run(self.ctx.on.config_changed(), state)

    def test_given_leader_when_configure_then_config_file_is_pushed(self):
        self.configure(self.ready_state(model=testing.Model(name="whatever")))

        expected_config = Path(f"tests/unit/config_{self.substrate.value}.hcl").read_text()
        assert hcl.loads(self.pushed_config()) == hcl.loads(expected_config)

    def test_given_peers_when_configure_then_retry_join_targets_the_active_peer(self):
        self.set_openbao_ready()
        peer_relation = testing.PeerRelation(
            endpoint="openbao-peers",
            peers_data={
                1: {"node_api_address": "https://1.2.3.4:8200"},
                2: {"node_api_address": "https://1.2.3.5:8200"},
            },
        )
        active_peer = "https://1.2.3.4:8200"

        def client(url: str | None = None, *args: object, **kwargs: object) -> MagicMock:
            if url in (active_peer, "https://1.2.3.5:8200"):
                return MagicMock(is_active=MagicMock(return_value=url == active_peer))
            return self.mock_openbao

        charm.OpenBaoClient.side_effect = client  # type: ignore[attr-defined]

        self.configure(self.ready_state(relations=[peer_relation]))

        retry_join = hcl.loads(self.pushed_config())["storage"]["raft"]["retry_join"]
        assert retry_join == {
            "leader_api_addr": "https://1.2.3.4:8200",
            "leader_ca_cert_file": f"{self.mock_workload.tls_directory_path}/ca.pem",
        }

    def test_given_leader_when_configure_then_address_is_shared_with_peers(self):
        state_out = self.configure(self.ready_state())

        peer_relation = state_out.get_relations("openbao-peers")[0]
        host = "myhostname" if self.substrate == Substrate.K8S else "1.2.1.2"
        assert peer_relation.local_unit_data["node_api_address"] == f"https://{host}:8200"

    def test_given_non_leader_without_peers_when_configure_then_openbao_is_not_configured(self):
        self.configure(self.ready_state(leader=False))

        self.mock_workload.push.assert_not_called()
        self.mock_workload.start.assert_not_called()

    def test_given_tls_files_not_in_workload_when_configure_then_openbao_is_not_started(self):
        self.mock_tls.tls_file_pushed_to_workload.return_value = False

        self.configure(self.ready_state())

        self.mock_workload.push.assert_not_called()
        self.mock_workload.start.assert_not_called()

    def test_given_nothing_changed_when_configure_then_openbao_is_started_not_restarted(self):
        self.set_openbao_ready()

        self.configure(self.ready_state())

        self.mock_workload.install.assert_called_once()
        self.mock_workload.start.assert_called_once()
        self.mock_workload.restart.assert_not_called()

    def test_given_service_environment_changed_when_configure_then_openbao_is_restarted(self):
        self.set_openbao_ready()
        self.mock_workload.configure_service.return_value = True

        self.configure(self.ready_state())

        self.mock_workload.restart.assert_called_once()
        self.mock_workload.start.assert_not_called()

    def test_given_sealed_with_transit_seal_when_configure_then_openbao_is_restarted_to_unseal(
        self,
    ):
        self.set_openbao_ready(is_sealed=True, is_seal_type_transit=True)

        self.configure(self.ready_state())

        self.mock_workload.restart.assert_called_once()
        self.mock_pki_manager.configure.assert_not_called()

    def test_given_proxy_environment_when_configure_then_service_environment_has_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("JUJU_CHARM_HTTP_PROXY", "http://proxy:3128")
        monkeypatch.setenv("JUJU_CHARM_NO_PROXY", "10.0.0.0/8")

        self.configure(self.ready_state())

        self.mock_workload.configure_service.assert_called_once_with(
            None, {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "10.0.0.0/8"}
        )

    # PKI and ACME

    @pytest.mark.parametrize(
        "endpoint, prefix, manager",
        [
            ("tls-certificates-pki", "pki", "mock_pki_manager"),
            ("tls-certificates-acme", "acme", "mock_acme_manager"),
        ],
    )
    def test_given_certificate_available_when_configure_then_ca_is_configured(
        self, endpoint: str, prefix: str, manager: str
    ):
        self.set_openbao_ready()
        relation = testing.Relation(endpoint=endpoint, interface="tls-certificates")
        self.mock_get_requirer_assigned_certificate.return_value = (
            generate_example_provider_certificate(
                common_name="myhostname.com",
                relation_id=relation.id,
                validity=timedelta(hours=24),
            )
        )

        self.configure(
            self.ready_state(relation, config={f"{prefix}_ca_common_name": "myhostname.com"})
        )

        getattr(self, manager).configure.assert_called_once()

    @pytest.mark.parametrize(
        "endpoint, prefix, manager, config_key",
        [
            ("tls-certificates-pki", "pki", "mock_pki_manager", "pki_allowed_domains"),
            ("tls-certificates-pki", "pki", "mock_pki_manager", "pki_ca_sans_dns"),
            ("tls-certificates-acme", "acme", "mock_acme_manager", "acme_allowed_domains"),
            ("tls-certificates-acme", "acme", "mock_acme_manager", "acme_ca_sans_dns"),
        ],
    )
    def test_given_ca_config_is_invalid_when_configure_then_ca_is_not_configured(
        self, endpoint: str, prefix: str, manager: str, config_key: str
    ):
        self.set_openbao_ready()
        relation = testing.Relation(endpoint=endpoint, interface="tls-certificates")
        config = {f"{prefix}_ca_common_name": "myhostname.com", config_key: NOT_A_LIST}

        self.configure(self.ready_state(relation, config=config))

        getattr(self, manager).configure.assert_not_called()

    # Auto-unseal

    def test_given_autounseal_details_available_when_configure_then_transit_seal_is_used(self):
        self.set_openbao_ready()
        self.mock_autounseal_requires_get_details.return_value = AUTOUNSEAL_DETAILS
        self.mock_openbao_autounseal_requirer_manager.get_provider_openbao_token.return_value = (
            "some token"
        )

        self.configure(self.ready_state())

        transit = hcl.loads(self.pushed_config())["seal"]["transit"]
        assert transit["address"] == "1.2.3.4"
        assert transit["mount_path"] == "charm-autounseal"
        assert transit["key_name"] == "key name"
        self.mock_tls.push_autounseal_ca_cert.assert_called_with("ca cert")
        self.mock_workload.configure_service.assert_called_once_with("some token", {})
        self.mock_openbao.authenticate.assert_called_with(AppRole("role id", "secret id"))

    def test_given_outstanding_autounseal_requests_when_configure_then_credentials_are_set(self):
        self.set_openbao_ready()
        autounseal_relation = testing.Relation(
            endpoint="openbao-autounseal-provides", interface="openbao-autounseal"
        )
        outstanding = self.mock_openbao_autounseal_provider_manager.get_outstanding_requests
        state_in = self.ready_state(
            autounseal_relation,
            networks=[
                testing.Network(
                    "openbao-autounseal-provides",
                    bind_addresses=[testing.BindAddress([testing.Address("5.6.7.8")])],
                    ingress_addresses=["5.6.7.8"],
                ),
                testing.Network(
                    "openbao-peers",
                    bind_addresses=[testing.BindAddress([testing.Address("1.2.1.2")])],
                ),
            ],
        )

        with self.ctx(self.ctx.on.config_changed(), state_in) as manager:
            relation = manager.charm.model.get_relation("openbao-autounseal-provides")
            outstanding.return_value = [relation]
            manager.run()

        self.mock_openbao.enable_secrets_engine.assert_called_once()
        self.mock_openbao_autounseal_provider_manager.create_credentials.assert_called_once_with(
            relation, "https://5.6.7.8:8200"
        )

    # KV

    def test_given_kv_request_when_configure_then_credentials_are_generated(self):
        self.set_openbao_ready()
        kv_relation = testing.Relation(
            endpoint="openbao-kv",
            interface="openbao-kv",
            remote_app_name="openbao-kv",
            remote_app_data={"mount_suffix": "remote-suffix"},
            remote_units_data={0: {"nonce": "123123", "egress_subnet": "2.2.2.0/24"}},
        )
        self.mock_tls.pull_tls_file_from_workload.return_value = "ca"

        self.configure(self.ready_state(kv_relation))

        kwargs = self.mock_kv_manager.generate_credentials_for_requirer.call_args.kwargs
        assert kwargs["relation"].id == kv_relation.id
        assert kwargs["app_name"] == "openbao-kv"
        assert kwargs["unit_name"] == "openbao-kv/0"
        assert kwargs["mount_suffix"] == "remote-suffix"
        assert kwargs["egress_subnets"] == ["2.2.2.0/24"]
        assert kwargs["nonce"] == "123123"
        assert kwargs["openbao_url"] == "https://192.0.2.0:8200"


@vm_only
class TestCharmConfigureHSM(OpenBaoCharmFixtures):
    """PKCS#11 auto-unseal, with the real hsm-lib installation of the machine workload."""

    @pytest.fixture(autouse=True)
    def real_hsm_lib(self, setup: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.hsm_dir = tmp_path / "hsm"
        self.openbao_env = tmp_path / "openbao.env"
        self.plugin_dir = tmp_path / "plugins"
        monkeypatch.setattr("workload_vm.HSM_LIB_DIR", str(self.hsm_dir))
        monkeypatch.setattr("workload_vm.OPENBAO_ENV_PATH", str(self.openbao_env))
        monkeypatch.setattr("workload_vm.PKCS11_KMS_PLUGIN_DIR", str(self.plugin_dir))
        monkeypatch.setattr(
            "workload_vm.PKCS11_KMS_PLUGIN_PATH",
            str(self.plugin_dir / "openbao-plugin-kms-pkcs11"),
        )
        monkeypatch.setattr(
            "workload_vm.PKCS11_KMS_PLUGIN_VERSION_PATH", str(self.plugin_dir / "pkcs11.version")
        )
        real = OpenBaoVMWorkload()
        self.mock_workload.install_hsm_lib.side_effect = real.install_hsm_lib
        self.mock_workload.pkcs11_kms_plugin.side_effect = real.pkcs11_kms_plugin
        self.tmp_path = tmp_path

    def install_kms_plugin(self) -> str:
        """Install a fake PKCS#11 KMS plugin in the snap and return its sha256sum."""
        self.plugin_dir.mkdir()
        plugin_bytes = b"fake-kms-plugin"
        (self.plugin_dir / "openbao-plugin-kms-pkcs11").write_bytes(plugin_bytes)
        (self.plugin_dir / "pkcs11.version").write_text("v0.1.0\n")
        return sha256(plugin_bytes).hexdigest()

    def hsm_lib_tarball(self, *names: str, openbao_env: str | None = None) -> Path:
        """Create a tarball of ELF stubs with the given names (default pkcs11.so)."""
        stage = self.tmp_path / "hsm-stage"
        stage.mkdir()
        for name in names or ("pkcs11.so",):
            (stage / name).write_bytes(b"\x7fELF" + b"\0" * 12)
        if openbao_env is not None:
            (stage / "openbao.env").write_text(openbao_env)
        archive = self.tmp_path / "hsm-lib.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for path in sorted(stage.iterdir()):
                tar.add(path, arcname=path.name)
        return archive

    def configure_hsm(
        self, hsm_lib: Path | None, secret_content: dict[str, str] | None = HSM_SECRET_CONTENT
    ) -> dict:
        """Run config-changed with the HSM secret and resource, and return the pushed config."""
        secrets = [testing.Secret(tracked_content=secret_content)] if secret_content else []
        state_in = self.ready_state(
            config={"hsm-config-secret-id": secrets[0].id} if secrets else {},
            secrets=secrets,
            resources=[testing.Resource(name="hsm-lib", path=hsm_lib)] if hsm_lib else [],
        )
        self.ctx.run(self.ctx.on.config_changed(), state_in)
        return hcl.loads(self.pushed_config())

    def test_given_hsm_secret_and_lib_when_configure_then_pkcs11_seal_is_used(self):
        plugin_sha = self.install_kms_plugin()
        hsm_lib = self.hsm_lib_tarball(
            "pkcs11.so",
            "libdep.so",
            openbao_env="export SOFTHSM2_CONF=/var/snap/openbao/common/hsm/softhsm2.conf\n",
        )
        content = {**HSM_SECRET_CONTENT, "token-label": "OpenBao", "key-id": "0x01"}

        config = self.configure_hsm(hsm_lib, content)

        assert (self.hsm_dir / "pkcs11.so").is_file()
        assert (self.hsm_dir / "libdep.so").is_file()
        assert "SOFTHSM2_CONF" in self.openbao_env.read_text()
        assert config["plugin_directory"] == str(self.plugin_dir)
        assert config["plugin_auto_register"] is True
        assert config["plugin"]["kms"]["pkcs11"] == {
            "command": "openbao-plugin-kms-pkcs11",
            "version": "v0.1.0",
            "sha256sum": plugin_sha,
        }
        assert config["seal"]["pkcs11"] == {
            "lib": str(self.hsm_dir / "pkcs11.so"),
            "slot": "0",
            "token_label": "OpenBao",
            "pin": "1234",
            "key_label": "bao-root-key",
            "key_id": "0x01",
        }

    def test_given_hsm_lib_secret_key_when_configure_then_named_module_is_used(self):
        self.install_kms_plugin()
        hsm_lib = self.hsm_lib_tarball("yubihsm_pkcs11.so", "libyubihsm.so.2")

        config = self.configure_hsm(hsm_lib, {**HSM_SECRET_CONTENT, "lib": "yubihsm_pkcs11.so"})

        assert config["seal"]["pkcs11"]["lib"] == str(self.hsm_dir / "yubihsm_pkcs11.so")

    def test_given_kms_plugin_missing_when_configure_then_no_seal_is_configured(self):
        assert "seal" not in self.configure_hsm(self.hsm_lib_tarball())

    def test_given_placeholder_hsm_lib_when_configure_then_no_seal_is_configured(self):
        placeholder = self.tmp_path / "placeholder.so"
        placeholder.write_text("placeholder")

        assert "seal" not in self.configure_hsm(placeholder)
        self.mock_workload.install_hsm_lib.assert_not_called()

    def test_given_hsm_lib_without_secret_when_configure_then_no_seal_is_configured(self):
        self.install_kms_plugin()

        assert "seal" not in self.configure_hsm(self.hsm_lib_tarball(), secret_content=None)
        self.mock_workload.install_hsm_lib.assert_not_called()

    def test_given_hsm_and_transit_autounseal_when_configure_then_neither_seal_is_used(self):
        self.install_kms_plugin()
        self.mock_autounseal_requires_get_details.return_value = AUTOUNSEAL_DETAILS

        assert "seal" not in self.configure_hsm(self.hsm_lib_tarball())

    def test_given_hsm_config_changed_when_configure_then_openbao_is_restarted(self):
        self.set_openbao_ready()
        self.install_kms_plugin()

        self.configure_hsm(self.hsm_lib_tarball())

        self.mock_workload.restart.assert_called_once()
