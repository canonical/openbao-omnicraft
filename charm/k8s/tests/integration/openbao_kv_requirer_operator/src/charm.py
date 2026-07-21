#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test charm for openbao-kv."""

import logging
import secrets
from pathlib import Path
from typing import Any

from charms.vault_k8s.v0.vault_kv import (
    VaultKvConnectedEvent,
    VaultKvReadyEvent,
    VaultKvRequires,
)
from openbao.juju_facade import JujuFacade, NoSuchStorageError
from ops import main
from ops.charm import ActionEvent, CharmBase
from ops.framework import EventBase
from ops.model import ActiveStatus

from openbao_client import OpenBaoClient

NONCE_SECRET_LABEL = "openbao-kv-nonce"
OPENBAO_KV_SECRET_LABEL = "openbao-kv"
OPENBAO_KV_SECRET_PATH = "test"
OPENBAO_CA_CERT_FILENAME = "ca.pem"


logger = logging.getLogger(__name__)


class OpenBaoKVRequirerCharm(CharmBase):
    """Charm requiring openbao-kv for testing."""

    def __init__(self, *args: Any):
        super().__init__(*args)
        self.openbao_kv = VaultKvRequires(self, "openbao-kv", mount_suffix="kv")
        self.juju_facade = JujuFacade(self)
        self.framework.observe(self.on.install, self._configure)
        self.framework.observe(self.on.update_status, self._configure)
        self.framework.observe(self.on.config_changed, self._configure)
        self.framework.observe(self.openbao_kv.on.connected, self._on_kv_connected)
        self.framework.observe(self.openbao_kv.on.ready, self._on_kv_ready)
        self.framework.observe(self.on.create_secret_action, self._on_create_secret_action)
        self.framework.observe(self.on.get_secret_action, self._on_get_secret_action)

    def _configure(self, _: EventBase):
        """Create a secret to store the nonce."""
        self.juju_facade.set_app_secret_content(
            label=NONCE_SECRET_LABEL,
            content={"nonce": secrets.token_hex(16)},
        )
        self.unit.status = ActiveStatus()

    def _on_kv_connected(self, event: VaultKvConnectedEvent):
        """Request credentials from OpenBao KV."""
        egress_subnets = self.juju_facade.get_egress_subnets(
            event.relation_name, relation=event.relation
        )
        self.openbao_kv.request_credentials(event.relation, egress_subnets, self.get_nonce())

    def _on_kv_ready(self, event: VaultKvReadyEvent):
        """Store the OpenBao KV credentials in a secret."""
        if not (relation := event.relation):
            return
        if not (ca_certificate := self.openbao_kv.get_ca_certificate(relation)):
            logger.error("CA certificate not found")
            return
        if not (openbao_url := self.openbao_kv.get_openbao_url(relation)):
            logger.error("OpenBao URL not found")
            return
        if not (mount := self.openbao_kv.get_mount(relation)):
            logger.error("Mount not found")
            return
        unit_credentials = self.openbao_kv.get_unit_credentials(relation)
        juju_secret_content = {
            "openbao-url": openbao_url,
            "mount": mount,
            "credentials-secret-id": unit_credentials,
        }
        self.juju_facade.set_app_secret_content(
            label=OPENBAO_KV_SECRET_LABEL, content=juju_secret_content
        )
        self._store_ca_certificate(cert=ca_certificate)

    def _store_ca_certificate(self, cert: str) -> None:
        """Store the CA certificate in the charm storage."""
        certs_path = self._get_ca_cert_location_in_charm()
        with open(f"{certs_path}/{OPENBAO_CA_CERT_FILENAME}", "w") as fd:
            fd.write(cert)

    def _on_create_secret_action(self, event: ActionEvent):
        """Create a secret in OpenBao KV."""
        if not self.juju_facade.secret_exists(label=OPENBAO_KV_SECRET_LABEL):
            event.fail("OpenBao KV secret not found")
            return
        kv_secret_content = self.juju_facade.get_latest_secret_content(
            label=OPENBAO_KV_SECRET_LABEL
        )
        mount = kv_secret_content["mount"]
        ca_certificate_path = self._get_ca_cert_location_in_charm()
        if ca_certificate_path is None:
            event.fail("CA certificate not found")
            return
        secret_key = event.params.get("key")
        secret_value = event.params.get("value")
        if not secret_key or not secret_value:
            event.fail("Missing key or value")
            return
        credentials_secret_content = self.juju_facade.get_latest_secret_content(
            id=kv_secret_content["credentials-secret-id"]
        )
        openbao = OpenBaoClient(
            url=kv_secret_content["openbao-url"],
            approle_role_id=credentials_secret_content["role-id"],
            ca_certificate=f"{ca_certificate_path}/{OPENBAO_CA_CERT_FILENAME}",
            approle_secret_id=credentials_secret_content["role-secret-id"],
        )
        openbao.create_secret_in_kv(
            path=OPENBAO_KV_SECRET_PATH, mount=mount, key=secret_key, value=secret_value
        )

    def _on_get_secret_action(self, event: ActionEvent) -> None:
        if not self.juju_facade.secret_exists(label=OPENBAO_KV_SECRET_LABEL):
            event.fail("OpenBao KV secret not found")
            return
        kv_secret_content = self.juju_facade.get_latest_secret_content(
            label=OPENBAO_KV_SECRET_LABEL
        )
        credentials_secret_content = self.juju_facade.get_latest_secret_content(
            id=kv_secret_content["credentials-secret-id"]
        )
        mount = kv_secret_content["mount"]
        ca_certificate_path = self._get_ca_cert_location_in_charm()
        if ca_certificate_path is None:
            event.fail("CA certificate not found")
            return
        secret_key = event.params.get("key")
        if not secret_key:
            event.fail("Missing key or value")
            return
        openbao = OpenBaoClient(
            url=kv_secret_content["openbao-url"],
            approle_role_id=credentials_secret_content["role-id"],
            ca_certificate=f"{ca_certificate_path}/{OPENBAO_CA_CERT_FILENAME}",
            approle_secret_id=credentials_secret_content["role-secret-id"],
        )
        openbao_secret = openbao.get_secret_in_kv(path=OPENBAO_KV_SECRET_PATH, mount=mount)
        if secret_key not in openbao_secret:
            event.fail("Secret not found")
            return
        event.set_results({"value": openbao_secret[secret_key]})

    def get_nonce(self) -> str:
        """Get the nonce from the secret."""
        secret = self.model.get_secret(label=NONCE_SECRET_LABEL)
        return secret.get_content(refresh=True)["nonce"]

    def _get_ca_cert_location_in_charm(self) -> Path | None:
        """Return the CA certificate location in the charm (not in the workload).

        This path would typically be: /var/lib/juju/storage/certs/0/ca.pem

        Returns:
            Path: The CA certificate location

        Raises:
            OpenBaoCertsError: If the CA certificate is not found
        """
        try:
            return self.juju_facade.get_storage_location("certs")
        except NoSuchStorageError:
            return None


if __name__ == "__main__":  # pragma: no cover
    main(OpenBaoKVRequirerCharm)
