# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Auto-unseal OpenBao with a PKCS#11 HSM."""

from pathlib import Path

from ops import CharmBase, ModelError

from common.juju_facade import (
    JujuFacade,
    NoSuchSecretError,
    SecretAccessDeniedError,
    SecretRemovedError,
    TransientJujuError,
)
from common.openbao_autounseal import OpenBaoAutounsealRequires
from common.openbao_helpers import (
    Pkcs11SealConfiguration,
    hsm_config_secret_validation_error,
    hsm_lib_module_relative_path,
    is_hsm_lib_resource_usable,
    pkcs11_seal_config_from_secret,
)
from core.base_workload import WorkloadBase
from literals import HSM_CONFIG_SECRET_ID_CONFIG_KEY, HSM_LIB_RESOURCE_NAME


class HSMManager:
    """Configure the PKCS#11 seal from the `hsm-config-secret-id` secret and `hsm-lib`."""

    def __init__(
        self,
        charm: CharmBase,
        workload: WorkloadBase,
        autounseal_requires: OpenBaoAutounsealRequires,
    ):
        self._charm = charm
        self._juju_facade = JujuFacade(charm)
        self._workload = workload
        self._autounseal_requires = autounseal_requires

    @property
    def requested(self) -> bool:
        """Return whether the operator has set the HSM secret config option."""
        return bool(self._juju_facade.get_string_config(HSM_CONFIG_SECRET_ID_CONFIG_KEY))

    def _transit_autounseal_requested(self) -> bool:
        """Return whether a transit auto-unseal provider relation is ready."""
        return self._autounseal_requires.get_details() is not None

    def get_config_error(self) -> str | None:
        """Return why the requested PKCS#11 seal cannot be configured, if it cannot."""
        if not self.requested:
            return None
        if self._transit_autounseal_requested():
            return "PKCS#11 HSM seal cannot be used together with transit auto-unseal"
        try:
            content = self._juju_facade.get_latest_secret_content(
                id=self._juju_facade.get_string_config(HSM_CONFIG_SECRET_ID_CONFIG_KEY)
            )
        except (NoSuchSecretError, SecretRemovedError, SecretAccessDeniedError):
            return "hsm-config secret is not accessible; grant it to the charm with `juju grant-secret`"
        except TransientJujuError:
            return None
        if error := hsm_config_secret_validation_error(content):
            return error
        if not self._get_hsm_lib_resource_path():
            return (
                "hsm-lib resource is not attached; pack the PKCS#11 module and deps "
                "into a tarball and use `juju attach-resource openbao hsm-lib=./hsm-lib.tar.gz`"
            )
        # Ensure the archive/module installs and the PKCS#11 .so can be resolved.
        if not self._install_hsm_lib():
            return (
                "hsm-lib could not be installed; ensure the tarball contains the PKCS#11 "
                "module (name it pkcs11.so or set secret key `lib` to the module filename)"
            )
        if not self._workload.pkcs11_kms_plugin():
            return (
                "OpenBao snap does not include the PKCS#11 KMS plugin "
                "(requires an amd64/arm64 snap revision that ships it)"
            )
        return None

    def get_seal_configuration(self) -> Pkcs11SealConfiguration | None:
        """Return PKCS#11 seal configuration when the secret, library, and KMS plugin are ready."""
        if not self.requested or self._transit_autounseal_requested():
            return None
        if not (lib_path := self._install_hsm_lib()):
            return None
        if not (content := self._get_hsm_secret_content()):
            return None
        if not (plugin := self._workload.pkcs11_kms_plugin()):
            return None
        plugin_directory, plugin_command, plugin_version, plugin_sha256sum = plugin
        return pkcs11_seal_config_from_secret(
            content,
            lib_path,
            plugin_directory=plugin_directory,
            plugin_command=plugin_command,
            plugin_version=plugin_version,
            plugin_sha256sum=plugin_sha256sum,
        )

    def _get_hsm_lib_resource_path(self) -> Path | None:
        """Return the attached hsm-lib path, or None if missing or a deploy placeholder.

        The resource is a directory packed as a tar/zip (module + shared-library
        dependencies), or a single ELF PKCS#11 `.so` for simple providers.
        Empty or non-ELF text placeholders used at deploy time count as unattached.
        """
        try:
            path = self._charm.model.resources.fetch(HSM_LIB_RESOURCE_NAME)
        except (ModelError, NameError, RuntimeError):
            return None
        if not path or not is_hsm_lib_resource_usable(Path(path)):
            return None
        return Path(path)

    def _install_hsm_lib(self) -> str | None:
        """Install the hsm-lib resource in the workload and return the PKCS#11 module path."""
        if not (resource := self._get_hsm_lib_resource_path()):
            return None
        content = self._get_hsm_secret_content() or {}
        return self._workload.install_hsm_lib(resource, hsm_lib_module_relative_path(content))

    def _get_hsm_secret_content(self) -> dict[str, str] | None:
        """Return the HSM secret content, or None if it cannot be read."""
        secret_id = self._juju_facade.get_string_config(HSM_CONFIG_SECRET_ID_CONFIG_KEY)
        if not secret_id:
            return None
        try:
            return self._juju_facade.get_latest_secret_content(id=secret_id)
        except (
            NoSuchSecretError,
            SecretRemovedError,
            SecretAccessDeniedError,
            TransientJujuError,
        ):
            return None
