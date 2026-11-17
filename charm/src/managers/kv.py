# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Give the openbao-kv requirers access to a KV secrets engine."""

import logging

from ops import CharmBase, Relation

from charms.vault_k8s.v0.vault_kv import VaultKvProvides
from common.juju_facade import (
    JujuFacade,
    NoSuchSecretError,
)
from common.openbao_client import (
    OpenBaoClient,
    OpenBaoClientError,
    SecretsBackend,
)
from literals import Naming

logger = logging.getLogger(__name__)


KV_POLICY = """# Allows the KV requirer to create, read, update, delete and list secrets
path "{mount}/*" {{
  capabilities = ["create", "read", "update", "delete", "list"]
}}
path "sys/internal/ui/mounts/{mount}" {{
  capabilities = ["read"]
}}
"""


class KVManager:
    """Encapsulates the business logic for managing KV credentials for requirer Charms."""

    def __init__(
        self,
        charm: CharmBase,
        openbao_client: OpenBaoClient,
        openbao_kv: VaultKvProvides,
        ca_cert: str,
    ):
        self._openbao_client = openbao_client
        self._juju_facade = JujuFacade(charm)
        self._openbao_kv = openbao_kv
        self._ca_cert = ca_cert

    def generate_credentials_for_requirer(
        self,
        relation: Relation,
        app_name: str,
        unit_name: str,
        mount_suffix: str,
        egress_subnets: list[str],
        nonce: str,
        openbao_url: str,
    ):
        """Generate KV credentials for the requirer, and store the credentials in the relation.

        This method ensures that the approle and policy are created or updated,
        and that the approle secret ID is generated and stored in a Juju secret.

        The Juju secret ID is then passed to the requirer, along with other
        necessary information to access the KV backend.

        Args:
            relation: The relation of the requirer
            app_name: The name of the requirer application
            unit_name: The name of the requirer unit for this relation
            mount_suffix: The suffix to append to the mount path, as provided by the requirer
            egress_subnets: The egress subnets of the requirer
            nonce: The nonce provided by the requirer
            openbao_url: The URL of the OpenBao server that the requirer can access
                over this relation.
        """
        if not self._juju_facade.is_leader:
            logger.debug("Only leader unit can handle a openbao-kv request")
            return
        mount = Naming.kv_mount_path(app_name, mount_suffix)
        self._openbao_client.enable_secrets_engine(SecretsBackend.KV_V2, mount)
        secret_id = self._ensure_unit_credentials(
            relation=relation,
            unit_name=unit_name,
            mount=mount,
            nonce=nonce,
            egress_subnets=egress_subnets,
        )
        self._openbao_kv.set_kv_data(
            relation=relation,
            mount=mount,
            ca_certificate=self._ca_cert,
            vault_url=openbao_url,
            nonce=nonce,
            credentials_juju_secret_id=secret_id,
        )
        self._remove_stale_nonce(relation=relation, nonce=nonce)

    def _remove_stale_nonce(self, relation: Relation, nonce: str) -> None:
        """Remove stale nonce.

        If the nonce is not present in the credentials, it is stale and should be removed.

        Args:
            relation: the reltaion from which to check the credentials for the nonce
            nonce: the one to remove if stale
        """
        credential_nonces = self._openbao_kv.get_credentials(relation).keys()
        if nonce not in set(credential_nonces):
            self._openbao_kv.remove_unit_credentials(relation, nonce=nonce)

    def _ensure_unit_credentials(
        self,
        relation: Relation,
        unit_name: str,
        mount: str,
        nonce: str,
        egress_subnets: list[str],
    ) -> str:
        """Ensure a unit has credentials to access the openbao-kv mount.

        If the credentials are already configured for the provided egress
        subnets, the existing Juju secret ID which contains the approle secret
        ID is returned.

        Otherwise, the necessary OpenBao policy and approle are created or
        updated as necessary.  A OpenBao secret ID is then generated for the
        approle and stored in a Juju secret. The secret is granted to the
        associate relation, and the ID of the Juju secret is returned.

        Returns:
            The ID of the Juju secret containing the approle secret ID.
        """
        policy_name = Naming.kv_policy_name(mount, unit_name)
        role_name = Naming.kv_role_name(mount, unit_name)

        juju_secret_label = Naming.kv_secret_label(unit_name=unit_name)
        current_credentials = self._openbao_kv.get_credentials(relation)
        credentials_juju_secret_id = current_credentials.get(nonce, "")

        if self._is_openbao_kv_role_configured(
            label=juju_secret_label,
            egress_subnets=egress_subnets,
            role_name=role_name,
            credentials_juju_secret_id=credentials_juju_secret_id,
        ):
            logger.info("OpenBao KV role already configured for the provided egress subnets")
            return credentials_juju_secret_id
        self._openbao_client.create_or_update_policy(policy_name, KV_POLICY.format(mount=mount))
        role_id = self._openbao_client.create_or_update_approle(
            role_name,
            policies=[policy_name],
            cidrs=egress_subnets,
            token_ttl="1h",
            token_max_ttl="1h",
        )
        role_secret_id = self._openbao_client.generate_role_secret_id(role_name, egress_subnets)
        secret = self._juju_facade.set_app_secret_content(
            content={"role-id": role_id, "role-secret-id": role_secret_id},
            label=juju_secret_label,
        )
        self._juju_facade.grant_secret(relation, secret=secret)
        if not secret.id:
            raise ValueError(
                f"Unexpected error, just created secret {juju_secret_label!r} has no id"
            )
        return secret.id

    def _is_openbao_kv_role_configured(
        self,
        label: str,
        egress_subnets: list[str],
        role_name: str,
        credentials_juju_secret_id: str,
    ) -> bool:
        """Check if the OpenBao role is already configured for the provided egress subnets.

        Args:
            label: The label of the secret
            egress_subnets: The egress subnets provided by the requirer.
            role_name: The role name associated with KV for this unit.
            credentials_juju_secret_id: The juju secret id.

        Returns:
            True if the role is already configured with the provided egress
        subnets, False otherwise.
        """
        try:
            role_secret_id = self._juju_facade.get_latest_secret_content(
                label=label,
                id=credentials_juju_secret_id,
            ).get("role-secret-id")
        except NoSuchSecretError:
            return False
        if not role_secret_id:
            return False
        try:
            role_data = self._openbao_client.read_role_secret(role_name, role_secret_id)
        except OpenBaoClientError:
            logger.warning(
                "Failed to read role secret for %s, will re-create credentials", role_name
            )
            return False
        if egress_subnets == role_data["cidr_list"]:
            return True
        return False

    @staticmethod
    def remove_unit_credentials(juju_facade: JujuFacade, unit_name: str) -> None:
        """Remove any KV credentials associated with the given unit.

        Args:
            juju_facade: The JujuFacade object to use for removing the secret
            unit_name: The name of the unit for which to remove the secret
        """
        juju_facade.remove_secret(Naming.kv_secret_label(unit_name=unit_name))
