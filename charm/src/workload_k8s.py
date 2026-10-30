#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""OpenBao workload running in a Kubernetes pod, managed through Pebble."""

from pathlib import Path
from typing import TextIO

from ops import Container as OpsContainer
from ops import ModelError
from ops.pebble import ChangeError, Layer, PathError

from core.base_workload import WorkloadBase, WorkloadError

SERVICE_NAME = "openbao"
# Raft peers reach each other by pod FQDN, which must never go through a proxy.
CLUSTER_LOCAL_DOMAIN = ".svc.cluster.local"


class OpenBaoK8sWorkload(WorkloadBase):
    """OpenBao running from the OCI image in the `openbao` container."""

    config_file_path = "/openbao/config/openbao.hcl"
    raft_storage_path = "/openbao/raft"
    tls_directory_path = "/openbao/certs"

    def __init__(self, container: OpsContainer):
        self._container = container

    def is_accessible(self) -> bool:
        """Check if we can connect to pebble."""
        return self._container.can_connect()

    def exists(self, path: str) -> bool:
        """Check if a file exists in the workload."""
        return self._container.exists(path=path)

    def pull(self, path: str) -> TextIO:
        """Read file from the workload."""
        return self._container.pull(path=path)

    def push(self, path: str, source: str) -> None:
        """Write file to the workload."""
        self._container.push(path=path, source=source)

    def make_dir(self, path: str) -> None:
        """Create directory in the workload."""
        self._container.make_dir(path=path, make_parents=True)

    def remove_path(self, path: str, recursive: bool = False) -> None:
        """Remove file or directory from the workload."""
        try:
            self._container.remove_path(path=path, recursive=recursive)
        except PathError as e:
            # Rebrand PathError to ValueError for consistency with the machine
            # implementation. The description of PathError satisfies the
            # definition of ValueError.
            raise ValueError(e) from e

    def install(self) -> None:
        """Do nothing: the OCI image ships OpenBao."""

    def configure_log_rotation(self, frequency: str) -> None:
        """Do nothing: OpenBao logs to stdout, which Kubernetes rotates."""

    def configure_service(
        self, autounseal_token: str | None, proxy_environment: dict[str, str]
    ) -> bool:
        """Add the Pebble layer of the OpenBao service if it differs from the current plan."""
        environment = dict(proxy_environment)
        if environment.get("HTTP_PROXY") or environment.get("HTTPS_PROXY"):
            no_proxy = [e.strip() for e in environment.get("NO_PROXY", "").split(",") if e.strip()]
            if CLUSTER_LOCAL_DOMAIN not in no_proxy:
                no_proxy.append(CLUSTER_LOCAL_DOMAIN)
            environment["NO_PROXY"] = ",".join(no_proxy)
        if autounseal_token:
            # VAULT_TOKEN is also set since OpenBao still honours the legacy variable name.
            environment["BAO_TOKEN"] = environment["VAULT_TOKEN"] = autounseal_token
        layer = Layer(
            {
                "summary": "openbao layer",
                "description": "pebble config layer for openbao",
                "services": {
                    SERVICE_NAME: {
                        "override": "replace",
                        "summary": "openbao",
                        "command": f"bao server -config={self.config_file_path}",
                        "startup": "enabled",
                        "environment": environment,
                    }
                },
            }
        )
        if self._container.get_plan().services == layer.services:
            return False
        self._container.add_layer(SERVICE_NAME, layer, combine=True)
        return True

    def start(self) -> None:
        """Start the services of the Pebble plan that are not running."""
        try:
            self._container.replan()
        except ChangeError as e:
            raise WorkloadError(str(e)) from e

    def restart(self) -> None:
        """Restart the openbao service."""
        self._container.restart(SERVICE_NAME)

    def stop(self) -> None:
        """Stop the openbao service."""
        try:
            self._container.stop(SERVICE_NAME)
        except ChangeError as e:
            raise WorkloadError(str(e)) from e

    def is_running(self) -> bool:
        """Return whether the openbao service is running."""
        try:
            return self._container.get_service(SERVICE_NAME).is_running()
        except ModelError:
            return False

    def install_hsm_lib(self, resource: Path, module_relative_path: str | None) -> str | None:
        """Return None: PKCS#11 HSM auto-unseal is only supported on machines."""
        return None

    def pkcs11_kms_plugin(self) -> tuple[str, str, str, str] | None:
        """Return None: the OCI image does not ship the PKCS#11 KMS plugin."""
        return None
