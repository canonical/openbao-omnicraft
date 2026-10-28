#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import subprocess
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from charms.operator_libs_linux.v2 import snap
from core.base_workload import WorkloadError
from workload_vm import OpenBaoVMWorkload


@pytest.fixture
def openbao_snap():
    with patch("workload_vm.snap.SnapCache") as snap_cache:
        openbao_snap = MagicMock(spec=snap.Snap)
        openbao_snap.services = {"server": {"active": False}}
        snap_cache.return_value = {"openbao": openbao_snap}
        yield openbao_snap


@pytest.fixture
def workload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OpenBaoVMWorkload:
    """Return the machine workload, with its files under tmp_path."""
    monkeypatch.setattr("workload_vm.HSM_LIB_DIR", str(tmp_path / "hsm"))
    monkeypatch.setattr("workload_vm.LOGROTATE_PATH", tmp_path / "logrotate")
    monkeypatch.setattr("workload_vm.OPENBAO_ENV_PATH", str(tmp_path / "openbao.env"))
    monkeypatch.setattr("workload_vm.SYSTEMD_CREDENTIAL_STORE", tmp_path / "credstore")
    monkeypatch.setattr("workload_vm.SYSTEMD_DROP_IN_DIR", str(tmp_path / "dropin"))
    monkeypatch.setattr(
        "workload_vm.SYSTEMD_DROP_IN_FILE_PATH", str(tmp_path / "dropin" / "10-charm.conf")
    )
    return OpenBaoVMWorkload()


class TestInstall:
    def test_given_pinned_revision_installed_when_install_then_snap_is_untouched(
        self, workload: OpenBaoVMWorkload, openbao_snap: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("workload_vm.SNAP_REVISION", "36")
        monkeypatch.setattr(OpenBaoVMWorkload, "make_dir", MagicMock())
        openbao_snap.revision = "36"
        openbao_snap.state = snap.SnapState.Latest

        workload.install()

        openbao_snap.ensure.assert_not_called()

    def test_given_snap_not_installed_when_install_then_it_is_installed_held_and_stopped(
        self, workload: OpenBaoVMWorkload, openbao_snap: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("workload_vm.SNAP_REVISION", "36")
        monkeypatch.setattr("workload_vm.SNAP_CHANNEL", "2/edge")
        monkeypatch.setattr(OpenBaoVMWorkload, "make_dir", MagicMock())
        openbao_snap.state = snap.SnapState.Available
        openbao_snap.services = {"server": {"active": True}}

        workload.install()

        openbao_snap.ensure.assert_called_once_with(
            snap.SnapState.Latest, channel="2/edge", revision="36"
        )
        openbao_snap.hold.assert_called_once()
        openbao_snap.stop.assert_called_once()

    def test_given_snap_error_when_install_then_workload_error_is_raised(
        self, workload: OpenBaoVMWorkload, openbao_snap: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(OpenBaoVMWorkload, "make_dir", MagicMock())
        openbao_snap.ensure.side_effect = snap.SnapError("no snapd")

        with pytest.raises(WorkloadError):
            workload.install()


class TestService:
    @pytest.mark.parametrize(
        "services, running",
        [
            ({"server": {"active": True}}, True),
            ({"server": {"active": False}}, False),
            ({}, False),
        ],
    )
    def test_when_is_running_then_snap_service_state_is_reported(
        self,
        workload: OpenBaoVMWorkload,
        openbao_snap: MagicMock,
        services: dict,
        running: bool,
    ):
        openbao_snap.services = services

        assert workload.is_running() is running

    def test_when_start_then_server_service_is_started(
        self, workload: OpenBaoVMWorkload, openbao_snap: MagicMock
    ):
        workload.start()

        openbao_snap.start.assert_called_once_with(services=["server"])

    @pytest.mark.parametrize("operation", ["start", "stop"])
    def test_given_snap_error_when_start_or_stop_then_workload_error_is_raised(
        self, workload: OpenBaoVMWorkload, openbao_snap: MagicMock, operation: str
    ):
        getattr(openbao_snap, operation).side_effect = snap.SnapError("boom")

        with pytest.raises(WorkloadError):
            getattr(workload, operation)()


class TestLogRotation:
    @pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly"])
    def test_given_valid_frequency_when_configure_log_rotation_then_syslog_rotation_is_written(
        self, workload: OpenBaoVMWorkload, tmp_path: Path, frequency: str
    ):
        workload.configure_log_rotation(frequency)

        content = (tmp_path / "logrotate").read_text()
        assert content.startswith("/var/log/syslog\n")
        assert f"    {frequency}\n" in content
        assert "rotate 7" in content
        assert "maxsize 10M" in content

    def test_given_invalid_frequency_when_configure_log_rotation_then_error_is_raised(
        self, workload: OpenBaoVMWorkload, tmp_path: Path
    ):
        with pytest.raises(ValueError):
            workload.configure_log_rotation("hourly")

        assert not (tmp_path / "logrotate").exists()


class TestConfigureService:
    @pytest.fixture(autouse=True)
    def subprocess_run(self):
        with patch("workload_vm.subprocess.run") as run:
            self.run = run
            yield

    def credentials_supported(self, supported: bool) -> None:
        def run(command: list, **kwargs: object) -> MagicMock:
            if command[:2] == ["systemd-creds", "setup"] and not supported:
                raise subprocess.CalledProcessError(1, command)
            return MagicMock(returncode=0, stdout="")

        self.run.side_effect = run

    def test_given_no_token_nor_proxy_when_configure_service_then_drop_in_is_removed(
        self, workload: OpenBaoVMWorkload, tmp_path: Path
    ):
        drop_in = tmp_path / "dropin" / "10-charm.conf"
        drop_in.parent.mkdir()
        drop_in.write_text("[Service]\n")
        (tmp_path / "openbao.env").write_text("export BAO_TOKEN=x\n")

        assert workload.configure_service(None, {}) is False

        assert not drop_in.exists()
        assert not (tmp_path / "openbao.env").exists()

    def test_given_token_and_credentials_supported_when_configure_service_then_token_is_a_credential(
        self, workload: OpenBaoVMWorkload, tmp_path: Path
    ):
        self.credentials_supported(True)

        assert workload.configure_service("my token", {"HTTP_PROXY": "http://proxy"}) is True

        drop_in = (tmp_path / "dropin" / "10-charm.conf").read_text()
        assert "LoadCredentialEncrypted=external_openbao_token" in drop_in
        assert 'Environment=HTTP_PROXY="http://proxy"' in drop_in
        assert "my token" not in drop_in
        assert "external_openbao_token" in (tmp_path / "openbao.env").read_text()
        encrypt = [
            c for c in self.run.call_args_list if c.args[0][:2] == ["systemd-creds", "encrypt"]
        ]
        assert encrypt[0].kwargs["input"] == "my token"
        self.run.assert_any_call(["systemctl", "daemon-reload"], check=True)

    def test_given_token_without_credentials_support_when_configure_service_then_token_is_in_environment(
        self, workload: OpenBaoVMWorkload, tmp_path: Path
    ):
        self.credentials_supported(False)

        assert workload.configure_service("my token", {}) is True

        drop_in = (tmp_path / "dropin" / "10-charm.conf").read_text()
        assert 'Environment=BAO_TOKEN="my token"' in drop_in
        assert 'Environment=VAULT_TOKEN="my token"' in drop_in

    def test_given_unchanged_environment_when_configure_service_then_nothing_changes(
        self, workload: OpenBaoVMWorkload
    ):
        self.credentials_supported(False)
        workload.configure_service(None, {"HTTPS_PROXY": "http://proxy"})
        self.run.reset_mock()

        assert workload.configure_service(None, {"HTTPS_PROXY": "http://proxy"}) is False
        assert ["systemctl", "daemon-reload"] not in [c.args[0] for c in self.run.call_args_list]


class TestHSMLib:
    def test_given_single_shared_object_when_install_hsm_lib_then_it_is_the_module(
        self, workload: OpenBaoVMWorkload, tmp_path: Path
    ):
        module = tmp_path / "libykcs11.so"
        module.write_bytes(b"\x7fELF" + b"\0" * 12)

        assert workload.install_hsm_lib(module, None) == str(tmp_path / "hsm" / "libykcs11.so")

    def test_given_archive_with_unsafe_path_when_install_hsm_lib_then_nothing_is_installed(
        self, workload: OpenBaoVMWorkload, tmp_path: Path
    ):
        archive = tmp_path / "hsm-lib.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escape.so", b"\x7fELF" + b"\0" * 12)

        assert workload.install_hsm_lib(archive, None) is None
        assert not (tmp_path / "escape.so").exists()

    def test_given_snap_ships_unsupported_plugin_when_pkcs11_kms_plugin_then_none(
        self, workload: OpenBaoVMWorkload, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (tmp_path / "plugin").write_bytes(b"plugin")
        (tmp_path / "version").write_text("unsupported\n")
        monkeypatch.setattr("workload_vm.PKCS11_KMS_PLUGIN_PATH", str(tmp_path / "plugin"))
        monkeypatch.setattr(
            "workload_vm.PKCS11_KMS_PLUGIN_VERSION_PATH", str(tmp_path / "version")
        )

        assert workload.pkcs11_kms_plugin() is None
