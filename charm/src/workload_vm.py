#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""OpenBao workload running on a machine, installed from the `openbao` snap."""

import hashlib
import logging
import os
import platform
import shutil
import subprocess
import tarfile
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import TextIO

from jinja2 import Environment, FileSystemLoader

from charms.operator_libs_linux.v2 import snap
from core.base_workload import WorkloadBase, WorkloadError
from openbao.openbao_helpers import (
    is_elf_shared_object,
    is_hsm_lib_archive,
    resolve_hsm_pkcs11_module,
)

logger = logging.getLogger(__name__)

SNAP_NAME = "openbao"
SNAP_SERVICE_NAME = "server"
# Bump SNAP_REVISIONS after publishing a snap that ships
# plugins/openbao-plugin-kms-pkcs11 (required for PKCS#11 HSM auto-unseal).
SNAP_REVISIONS = {
    "x86_64": "36",
}
SNAP_CHANNELS = {
    "x86_64": "2/edge",
}
SNAP_REVISION = SNAP_REVISIONS.get(platform.machine(), "")
SNAP_CHANNEL = SNAP_CHANNELS.get(platform.machine(), "2/stable")
SNAP_COMMON = "/var/snap/openbao/common"
# baod-start sources this file before starting OpenBao.
OPENBAO_ENV_PATH = f"{SNAP_COMMON}/openbao.env"
HSM_LIB_DIR = f"{SNAP_COMMON}/hsm"
PKCS11_KMS_PLUGIN_DIR = "/snap/openbao/current/plugins"
PKCS11_KMS_PLUGIN_COMMAND = "openbao-plugin-kms-pkcs11"
PKCS11_KMS_PLUGIN_PATH = f"{PKCS11_KMS_PLUGIN_DIR}/{PKCS11_KMS_PLUGIN_COMMAND}"
PKCS11_KMS_PLUGIN_VERSION_PATH = f"{PKCS11_KMS_PLUGIN_DIR}/pkcs11.version"
LOGROTATE_PATH = Path("/etc/logrotate.d/rsyslog")
LOGROTATE_FREQUENCIES = ("daily", "weekly", "monthly")
LOGROTATE_DEFAULT_COUNT = 7
LOGROTATE_DEFAULT_MAXSIZE = "10M"
LOGROTATE_TEMPLATE = """\
/var/log/syslog
{{
    rotate {rotate_count}
    {frequency}
    maxsize {maxsize}
    missingok
    notifempty
    compress
    delaycompress
    postrotate
        /usr/lib/rsyslog/rsyslog-rotate
    endscript
}}
"""
SYSTEMD_DROP_IN_DIR = "/etc/systemd/system/snap.openbao.server.service.d"
SYSTEMD_DROP_IN_FILE_PATH = f"{SYSTEMD_DROP_IN_DIR}/10-charm.conf"
SYSTEMD_CREDENTIAL_STORE = Path("/etc/credstore.encrypted/")
SYSTEMD_CRED_AUTOUNSEAL_TOKEN_NAME = "external_openbao_token"
TEMPLATE_PATH = "src/templates/"
TEMPLATE_SYSTEMD_DROP_IN = "systemd_dropin_creds.conf.j2"
TEMPLATE_OPENBAO_ENV_LOAD_SYSTEMD_CREDS = "openbao_load_systemd_creds.env.j2"


class OpenBaoVMWorkload(WorkloadBase):
    """OpenBao running as the `server` service of the `openbao` snap."""

    config_file_path = f"{SNAP_COMMON}/openbao-config.hcl"
    raft_storage_path = f"{SNAP_COMMON}/raft"
    tls_directory_path = f"{SNAP_COMMON}/certs"

    def is_accessible(self) -> bool:
        """Return True: the machine is always accessible to the charm running on it."""
        return True

    def exists(self, path: str) -> bool:
        """Check if a file exists.

        Args:
            path: The path of the file

        Returns:
            bool: Whether the file exists
        """
        return os.path.isfile(path)

    def pull(self, path: str) -> TextIO:
        """Get the content of a file.

        Args:
            path: The path of the file

        Returns:
            str: The content of the file
        """
        return open(path, "r")

    def push(self, path: str, source: str) -> None:
        """Pushes a file to the unit.

        Args:
            path: The path of the file
            source: The contents of the file to be pushed
        """
        with open(path, "w") as write_file:
            write_file.write(source)
            logger.info("Pushed file %s", path)

    def _copy_file(self, source: str, dest: str) -> None:
        """Copy a file on the unit, preserving metadata.

        Args:
            source: The path of the source file
            dest: The path of the destination file
        """
        shutil.copy2(source, dest)
        os.chmod(dest, 0o755)
        logger.info("Copied file %s to %s", source, dest)

    def make_dir(self, path: str) -> None:
        """Create a directory."""
        Path(path).mkdir(parents=True, exist_ok=True)

    def _replace_directory(self, path: str) -> None:
        """Remove path if it exists and recreate it as an empty directory."""
        destination = Path(path)
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        destination.mkdir(parents=True, exist_ok=True)

    def _extract_archive(self, archive: str, dest: str) -> None:
        """Extract a tar/zip archive into dest (dest must already exist)."""
        archive_path = Path(archive)
        dest_path = Path(dest)
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as zf:
                for info in zf.infolist():
                    member_path = Path(info.filename)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise ValueError(f"Refusing unsafe zip member path: {info.filename}")
                zf.extractall(dest_path)
        elif tarfile.is_tarfile(archive_path):
            with tarfile.open(archive_path) as tf:
                members = tf.getmembers()
                for member in members:
                    member_path = Path(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise ValueError(f"Refusing unsafe tar member path: {member.name}")
                # filter="data" is the safe default on Python 3.12+
                try:
                    tf.extractall(dest_path, members=members, filter="data")
                except TypeError:
                    tf.extractall(dest_path, members=members)
        else:
            raise ValueError(f"Unsupported hsm-lib archive format: {archive}")
        logger.info("Extracted archive %s to %s", archive, dest)

    def remove_path(self, path: str, recursive: bool = False) -> None:
        """Remove a file or directory.

        Args:
            path: The absolute path of the file or directory
            recursive: Whether to remove recursively
        raises:
            ValueError: If the path is not absolute.
        """
        if not os.path.isabs(path):
            raise ValueError(f"The provided path is not absolute: {path}")
        if os.path.isdir(path) and recursive:
            shutil.rmtree(path)
            logger.debug("Recursively removed directory `%s`", path)
        elif os.path.isfile(path) or (os.path.isdir(path) and not recursive):
            os.remove(path)
            logger.debug("Removed file or directory `%s`", path)
        else:
            raise ValueError(f"Path `{path}` does not exist.")

    def install(self) -> None:
        """Create OpenBao's directories and install the pinned revision of the snap."""
        self.make_dir(self.raft_storage_path)
        self.make_dir(self.tls_directory_path)
        try:
            openbao_snap = snap.SnapCache()[SNAP_NAME]
            if SNAP_REVISION == openbao_snap.revision and openbao_snap.state in [
                snap.SnapState.Latest,
                snap.SnapState.Present,
            ]:
                logger.debug("OpenBao snap revision %s is already installed", SNAP_REVISION)
                return
            openbao_snap.ensure(
                snap.SnapState.Latest, channel=SNAP_CHANNEL, revision=SNAP_REVISION
            )
            openbao_snap.hold()
            logger.info("OpenBao snap installed")
            # The snap starts OpenBao before the charm has configured it.
            if self.is_running():
                openbao_snap.stop()
                logger.debug("Previously running OpenBao service stopped")
        except snap.SnapError as e:
            raise WorkloadError(f"Failed to install the OpenBao snap: {e}") from e

    def configure_log_rotation(self, frequency: str) -> None:
        """Write the logrotate configuration of syslog, where the snap logs.

        Raises:
            ValueError: If the frequency is not daily, weekly or monthly.
        """
        if frequency not in LOGROTATE_FREQUENCIES:
            raise ValueError(f"Invalid logrotate frequency: {frequency}")
        LOGROTATE_PATH.write_text(
            LOGROTATE_TEMPLATE.format(
                rotate_count=LOGROTATE_DEFAULT_COUNT,
                frequency=frequency,
                maxsize=LOGROTATE_DEFAULT_MAXSIZE,
            )
        )

    def configure_service(
        self, autounseal_token: str | None, proxy_environment: dict[str, str]
    ) -> bool:
        """Add or remove the systemd drop-in that sets the environment of the snap service.

        The auto-unseal token is passed as an encrypted systemd credential when the
        machine supports it, and as a plain environment variable otherwise.
        `openbao.env` is restored from an optional `openbao.env` shipped in the
        hsm-lib resource when present; otherwise it is cleared.
        """
        if not autounseal_token and not proxy_environment:
            logger.debug("No auto-unseal token or proxy environment variables available")
            with suppress(ValueError):
                self.remove_path(SYSTEMD_DROP_IN_FILE_PATH)
            if self._apply_hsm_openbao_env():
                return True
            with suppress(ValueError):
                self.remove_path(OPENBAO_ENV_PATH)
                logger.info("Removed systemd drop-in file and openbao.env")
            return False

        if autounseal_token:
            try:
                _encrypt_credential_if_changed(
                    SYSTEMD_CRED_AUTOUNSEAL_TOKEN_NAME, autounseal_token
                )
            except subprocess.CalledProcessError:
                logger.warning("Failed to encrypt auto-unseal token")

        if self._write_systemd_drop_in(autounseal_token, proxy_environment):
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            return True
        return False

    def _write_systemd_drop_in(
        self, autounseal_token: str | None, proxy_environment: dict[str, str]
    ) -> bool:
        """Write the systemd drop-in, and `openbao.env` when the token is a credential.

        Returns:
            True if the drop-in changed, False otherwise
        """
        jinja2 = Environment(loader=FileSystemLoader(TEMPLATE_PATH))
        self.make_dir(path=SYSTEMD_DROP_IN_DIR)
        credentials_supported = _systemd_credentials_supported()
        if autounseal_token and not credentials_supported:
            logger.warning(
                "This system configuration does not support systemd credentials. Falling back to un-encrypted environment variables."
            )
        dropin_content = jinja2.get_template(TEMPLATE_SYSTEMD_DROP_IN).render(
            credential_name=SYSTEMD_CRED_AUTOUNSEAL_TOKEN_NAME
            if autounseal_token and credentials_supported
            else None,
            external_openbao_token=autounseal_token
            if autounseal_token and not credentials_supported
            else None,
            proxy_env=proxy_environment,
        )
        if (
            self.exists(path=SYSTEMD_DROP_IN_FILE_PATH)
            and self.pull(path=SYSTEMD_DROP_IN_FILE_PATH).read() == dropin_content
        ):
            return False
        self.push(path=SYSTEMD_DROP_IN_FILE_PATH, source=dropin_content)

        # A credential is only loaded into BAO_TOKEN by openbao.env.
        if autounseal_token and credentials_supported:
            openbao_env_content = jinja2.get_template(
                TEMPLATE_OPENBAO_ENV_LOAD_SYSTEMD_CREDS
            ).render(credential_name=SYSTEMD_CRED_AUTOUNSEAL_TOKEN_NAME)
            # Preserve optional provider exports shipped in hsm-lib's openbao.env.
            if not self._apply_hsm_openbao_env(prefix=openbao_env_content):
                self.push(path=OPENBAO_ENV_PATH, source=openbao_env_content)
            logger.info("Updated systemd drop-in file for OpenBao service")
        return True

    def _apply_hsm_openbao_env(self, prefix: str = "") -> bool:
        """Install the optional `openbao.env` of the unpacked hsm-lib into SNAP_COMMON.

        Provider-specific exports can be shipped as `openbao.env` inside the hsm-lib
        tarball; they are copied into place without parsing HSM-specific keys.

        Args:
            prefix: Content written before the hsm-lib openbao.env (e.g. transit
                credential exports).

        Returns:
            True if `OPENBAO_ENV_PATH` was written or changed.
        """
        hsm_env_path = f"{HSM_LIB_DIR}/openbao.env"
        if not self.exists(hsm_env_path):
            return False
        hsm_content = self.pull(path=hsm_env_path).read()
        content = f"{prefix.rstrip()}\n{hsm_content}" if prefix else hsm_content
        if not content.endswith("\n"):
            content += "\n"
        if self.exists(OPENBAO_ENV_PATH) and self.pull(OPENBAO_ENV_PATH).read() == content:
            return False
        self.push(path=OPENBAO_ENV_PATH, source=content)
        logger.info("Installed openbao.env from hsm-lib into %s", OPENBAO_ENV_PATH)
        return True

    def start(self) -> None:
        """Start the OpenBao snap service."""
        try:
            snap.SnapCache()[SNAP_NAME].start(services=[SNAP_SERVICE_NAME])
        except snap.SnapError as e:
            raise WorkloadError(f"Failed to start OpenBao: {e}") from e

    def restart(self) -> None:
        """Restart the services of the OpenBao snap."""
        snap.SnapCache()[SNAP_NAME].restart()
        logger.debug("OpenBao service restarted")

    def stop(self) -> None:
        """Stop the services of the OpenBao snap."""
        try:
            snap.SnapCache()[SNAP_NAME].stop()
        except snap.SnapError as e:
            raise WorkloadError(f"Failed to stop OpenBao: {e}") from e

    def is_running(self) -> bool:
        """Return whether the OpenBao snap service is active."""
        service = snap.SnapCache()[SNAP_NAME].services.get(SNAP_SERVICE_NAME)
        return bool(service and service["active"])

    def install_hsm_lib(self, resource: Path, module_relative_path: str | None) -> str | None:
        """Unpack the hsm-lib resource into SNAP_COMMON, where the snap can read it."""
        try:
            self._replace_directory(HSM_LIB_DIR)
            if is_hsm_lib_archive(resource):
                self._extract_archive(str(resource), HSM_LIB_DIR)
            elif is_elf_shared_object(resource):
                self._copy_file(str(resource), f"{HSM_LIB_DIR}/{resource.name}")
            else:
                return None
        except (OSError, ValueError) as exc:
            logger.warning("Failed to install hsm-lib resource: %s", exc)
            return None
        self._apply_hsm_openbao_env()
        module = resolve_hsm_pkcs11_module(Path(HSM_LIB_DIR), module_relative_path)
        if not module:
            logger.warning(
                "Could not resolve PKCS#11 module under %s (set hsm-config secret key `lib`)",
                HSM_LIB_DIR,
            )
            return None
        return str(module)

    def pkcs11_kms_plugin(self) -> tuple[str, str, str] | None:
        """Return the plugin metadata if the snap revision ships the PKCS#11 KMS plugin."""
        plugin_path = Path(PKCS11_KMS_PLUGIN_PATH)
        if not plugin_path.is_file():
            return None
        try:
            version = Path(PKCS11_KMS_PLUGIN_VERSION_PATH).read_text(encoding="utf-8").strip()
            sha256sum = hashlib.sha256(plugin_path.read_bytes()).hexdigest()
        except OSError:
            return None
        if not version or version == "unsupported":
            return None
        return (PKCS11_KMS_PLUGIN_DIR, version, sha256sum)


def _systemd_credentials_supported() -> bool:
    """Return whether the machine supports encrypted systemd credentials."""
    try:
        subprocess.run(["systemd-creds", "setup"], check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def _encrypt_credential_if_changed(name: str, value: str) -> None:
    """Store a value in the encrypted systemd credential store, if it changed.

    The value is then loadable by name with `LoadCredentialEncrypted` in a unit file.
    """
    path = SYSTEMD_CREDENTIAL_STORE / name
    if path.exists():
        decrypted = subprocess.run(
            ["systemd-creds", "decrypt", f"--name={name}", path], capture_output=True, text=True
        )
        if decrypted.returncode != 0:
            logger.error(
                "Failed to decrypt credential %s. stderr: %s", name, decrypted.stderr.strip()
            )
            raise RuntimeError("Token decryption failed")
        if decrypted.stdout.strip() == value:
            logger.debug("Token %s has not changed, skipping encryption.", name)
            return
    subprocess.run(
        ["systemd-creds", "encrypt", f"--name={name}", "-", path],
        input=value,
        text=True,
        check=True,
    )
    logger.debug("Token %s has been encrypted and stored.", name)
