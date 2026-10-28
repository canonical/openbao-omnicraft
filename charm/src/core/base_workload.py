# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Interface to the OpenBao workload, implemented once per substrate."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TextIO


class WorkloadError(Exception):
    """Raised when the workload cannot be installed, started or stopped."""


class WorkloadBase(ABC):
    """Operations the charm needs from OpenBao, whether it runs in a pod or on a machine."""

    config_file_path: str
    raft_storage_path: str
    tls_directory_path: str

    @abstractmethod
    def is_accessible(self) -> bool:
        """Return whether the workload can be reached (always True on machines)."""

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Return whether a file exists in the workload."""

    @abstractmethod
    def pull(self, path: str) -> TextIO:
        """Read a file from the workload."""

    @abstractmethod
    def push(self, path: str, source: str) -> None:
        """Write a file to the workload."""

    @abstractmethod
    def make_dir(self, path: str) -> None:
        """Create a directory, and its parents, in the workload."""

    @abstractmethod
    def remove_path(self, path: str, recursive: bool = False) -> None:
        """Remove a file or directory from the workload.

        Raises:
            ValueError: If the path is not absolute, or the path does not exist.
        """

    @abstractmethod
    def install(self) -> None:
        """Make sure OpenBao is installed and its directories exist.

        Raises:
            WorkloadError: If OpenBao cannot be installed.
        """

    @abstractmethod
    def configure_log_rotation(self, frequency: str) -> None:
        """Configure how often the OpenBao logs are rotated."""

    @abstractmethod
    def configure_service(
        self, autounseal_token: str | None, proxy_environment: dict[str, str]
    ) -> bool:
        """Apply the environment of the OpenBao service.

        Args:
            autounseal_token: Token for the transit auto-unseal provider, if any.
            proxy_environment: HTTP_PROXY, HTTPS_PROXY and NO_PROXY values to set.

        Returns:
            Whether the environment changed, so a running service must restart to use it.
        """

    @abstractmethod
    def start(self) -> None:
        """Start the OpenBao service if it is not running.

        Raises:
            WorkloadError: If the service cannot be started.
        """

    @abstractmethod
    def restart(self) -> None:
        """Restart the OpenBao service."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the OpenBao service.

        Raises:
            WorkloadError: If the service cannot be stopped.
        """

    @abstractmethod
    def is_running(self) -> bool:
        """Return whether the OpenBao service is running."""

    @abstractmethod
    def install_hsm_lib(self, resource: Path, module_relative_path: str | None) -> str | None:
        """Install the PKCS#11 library from the `hsm-lib` resource.

        Args:
            resource: The attached resource: an archive of the library and its
                dependencies, or a single shared object.
            module_relative_path: The PKCS#11 module inside the archive, if set.

        Returns:
            The path of the PKCS#11 module in the workload, or None if it cannot be installed.
        """

    @abstractmethod
    def pkcs11_kms_plugin(self) -> tuple[str, str, str, str] | None:
        """Return the (directory, command, version, sha256sum) of the PKCS#11 KMS plugin."""
