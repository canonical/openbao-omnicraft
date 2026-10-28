# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Provide and consume transit auto-unseal."""

import logging

from ops import CharmBase, Relation

from common.juju_facade import (
    FacadeError,
    JujuFacade,
    NoSuchSecretError,
    SecretRemovedError,
)
from common.openbao_autounseal import (
    AutounsealDetails,
    OpenBaoAutounsealProvides,
    OpenBaoAutounsealRequires,
)
from common.openbao_client import (
    AppRole,
    OpenBaoAuthenticationError,
    OpenBaoClient,
    OpenBaoClientError,
    Token,
)
from literals import Naming

logger = logging.getLogger(__name__)


AUTOUNSEAL_POLICY = """path "{mount}/encrypt/{key_name}" {{
    capabilities = ["update"]
}}

path "{mount}/decrypt/{key_name}" {{
    capabilities = ["update"]
}}
"""


class AutounsealProviderManager:
    """Encapsulates the auto-unseal functionality.

    This class provides the business logic for auto-unseal functionality in
    OpenBao charms. It is opinionated, and aims to make the interface to enabling
    and managing the feature as simple as possible. Flexibility is sacrificed
    for simplicity.
    """

    def __init__(
        self,
        charm: CharmBase,
        client: OpenBaoClient,
        provides: OpenBaoAutounsealProvides,
        ca_cert: str,
        mount_path: str,
    ):
        self._juju_facade = JujuFacade(charm)
        self._model = charm.model
        self._client = client
        self._provides = provides
        self._mount_path = mount_path
        self._ca_cert = ca_cert

    @property
    def mount_path(self) -> str:
        """Return the mount path for the transit backend."""
        return self._mount_path

    def clean_up_credentials(self) -> None:
        """Clean up roles and policies that are no longer needed by autounseal.

        This method will remove any roles and policies that are no longer
        used by any of the existing relations. It will also detect any orphaned
        keys (keys that are not associated with any relation) and log a warning.
        """
        self._clean_up_roles()
        self._clean_up_policies()
        self._detect_and_allow_deletion_of_orphaned_keys()

    def _detect_and_allow_deletion_of_orphaned_keys(self) -> None:
        """Detect and allow deletion of autounseal keys that are no longer associated with a Juju autounseal relation.

        The keys themselves are not deleted. This is to prevent an
        unrecoverable state if a relation is removed by mistake, or before
        migrating the data to a different seal type.

        The keys are marked as `allow_deletion` in openbao. This allows the user
        to manually delete the keys using the OpenBao CLI if they are sure the
        keys are no longer needed.

        A warning is logged so that the Juju operator is aware of the orphaned
        keys and can act accordingly.
        """
        existing_keys = self._get_existing_keys()
        relation_key_names = [
            Naming.autounseal_key_name(relation.id)
            for relation in self._juju_facade.get_active_relations(self._provides.relation_name)
        ]
        orphaned_keys = [key for key in existing_keys if key not in relation_key_names]
        if not orphaned_keys:
            return
        logger.warning(
            "Orphaned autounseal keys were detected: %s. If you are sure these are no longer needed, you may manually delete them using the openbao CLI to suppress this message. To delete a key, use the command `openbao delete %s/keys/<key_name>`.",
            orphaned_keys,
            self.mount_path,
        )
        for key_name in orphaned_keys:
            deletion_allowed = self._is_deletion_allowed(key_name)
            if not deletion_allowed:
                self._allow_key_deletion(key_name)

    def _allow_key_deletion(self, key_name: str) -> None:
        self._client.write(f"{self.mount_path}/keys/{key_name}/config", {"deletion_allowed": True})
        logger.info("Key marked as `deletion_allowed`: %s", key_name)

    def _is_deletion_allowed(self, key_name: str) -> bool:
        data = self._client.read(f"{self.mount_path}/keys/{key_name}")
        return data["deletion_allowed"]

    def _clean_up_roles(self) -> None:
        """Delete roles that are no longer associated with an autounseal Juju relation."""
        existing_roles = self._get_existing_roles()
        relation_role_names = [
            Naming.autounseal_approle_name(relation.id)
            for relation in self._juju_facade.get_active_relations(self._provides.relation_name)
        ]
        for role in existing_roles:
            if role not in relation_role_names:
                self._client.delete_role(role)
                logger.info("Removed unused role: %s", role)

    def _clean_up_policies(self) -> None:
        """Delete policies that are no longer associated with an autounseal Juju relation."""
        existing_policies = self._get_existing_policies()
        relation_policy_names = [
            Naming.autounseal_policy_name(relation.id)
            for relation in self._juju_facade.get_active_relations(self._provides.relation_name)
        ]
        for policy in existing_policies:
            if policy not in relation_policy_names:
                self._client.delete_policy(policy)
                logger.info("Removed unused policy: %s", policy)

    def _create_key(self, key_name: str) -> None:
        response = self._client.create_transit_key(mount_point=self.mount_path, key_name=key_name)
        logger.debug("Created a new autounseal key: %s", response)

    def create_credentials(self, relation: Relation, openbao_address: str) -> tuple[str, str, str]:
        """Create auto-unseal credentials for the given relation.

        Args:
            relation: The relation to create the credentials for.
            openbao_address: The address where this relation can reach the OpenBao.

        Returns:
            A tuple containing the key name, role ID, and approle secret ID.
        """
        key_name = Naming.autounseal_key_name(relation.id)
        policy_name = Naming.autounseal_policy_name(relation.id)
        approle_name = Naming.autounseal_approle_name(relation.id)
        self._create_key(key_name)
        policy_content = AUTOUNSEAL_POLICY.format(mount=self.mount_path, key_name=key_name)
        self._client.create_or_update_policy(
            policy_name,
            policy_content,
        )
        role_id = self._client.create_or_update_approle(
            approle_name,
            policies=[policy_name],
            token_period="60s",
        )
        secret_id = self._client.generate_role_secret_id(approle_name)
        self._provides.set_autounseal_data(
            relation,
            openbao_address,
            self.mount_path,
            key_name,
            role_id,
            secret_id,
            self._ca_cert,
        )
        return key_name, role_id, secret_id

    def get_outstanding_requests(self) -> list[Relation]:
        """Get relations that need credentials created or re-created.

        This includes relations that:
        - Have no credentials issued yet (new requests)
        - Have credentials in Juju that are stale in OpenBao (e.g. after a
          Raft snapshot restore)

        Returns:
            A list of relations that need credentials to be (re-)created.
        """
        outstanding: list[Relation] = []
        all_relations = self._juju_facade.get_active_relations(self._provides.relation_name)
        for relation in all_relations:
            try:
                credentials_secret_id = self._juju_facade.get_app_relation_data(
                    self._provides.relation_name, relation.id
                ).get("credentials_secret_id")
            except FacadeError:
                outstanding.append(relation)
                continue
            if not credentials_secret_id:
                outstanding.append(relation)
                continue
            try:
                _, secret_id = self._juju_facade.get_secret_content_values(
                    "role-id",
                    "secret-id",
                    id=credentials_secret_id,
                )
            except (NoSuchSecretError, SecretRemovedError):
                outstanding.append(relation)
                continue
            if not secret_id:
                outstanding.append(relation)
                continue
            try:
                approle_name = Naming.autounseal_approle_name(relation.id)
                self._client.read_role_secret(approle_name, secret_id)
            except OpenBaoClientError:
                logger.warning(
                    "Autounseal credentials for relation %s are stale, will re-create",
                    relation.id,
                )
                outstanding.append(relation)
        return outstanding

    def _get_existing_keys(self) -> list[str]:
        return self._client.list(f"{self.mount_path}/keys")

    def _get_existing_roles(self) -> list[str]:
        output = self._client.list("auth/approle/role")
        return [role for role in output if role.startswith(Naming.autounseal_approle_prefix)]

    def _get_existing_policies(self) -> list[str]:
        output = self._client.list("sys/policy")
        return [policy for policy in output if policy.startswith(Naming.autounseal_policy_prefix)]


class AutounsealRequirerManager:
    """Encapsulates the auto-unseal functionality from the Requirer Perspective.

    In other words, this manages the feature from the perspective of the OpenBao
    being auto-unsealed.
    """

    AUTOUNSEAL_TOKEN_SECRET_LABEL = "openbao-autounseal-token"

    def __init__(
        self,
        charm: CharmBase,
        requires: OpenBaoAutounsealRequires,
    ):
        self._juju_facade = JujuFacade(charm)
        self._requires = requires

    def get_provider_openbao_token(
        self, autounseal_details: AutounsealDetails, ca_cert_path: str
    ) -> str:
        """Retrieve the auto-unseal OpenBao token, or generate a new one if required.

        Retrieves the last used token from Juju secrets, and validates that it
        is still valid. If the token is not valid, a new token is generated and
        stored in the Juju secret. A valid token is returned.

        Args:
            autounseal_details: The autounseal configuration details.
            ca_cert_path: The path to the CA certificate to validate the provider OpenBao.

        Returns:
            A periodic OpenBao token that can be used for auto-unseal.

        """
        external_openbao = OpenBaoClient(url=autounseal_details.address, ca_cert_path=ca_cert_path)
        try:
            existing_token = self._juju_facade.get_secret_content_values(
                "token", label=self.AUTOUNSEAL_TOKEN_SECRET_LABEL
            )[0]
        except FacadeError:
            existing_token = None
        # If we don't already have a token, or if the existing token is invalid,
        # authenticate with the AppRole details to generate a new token.
        try:
            token_valid = existing_token and external_openbao.authenticate(Token(existing_token))
        except OpenBaoAuthenticationError:
            token_valid = False
        if not token_valid:
            external_openbao.authenticate(
                AppRole(autounseal_details.role_id, autounseal_details.secret_id)
            )
            # NOTE: This is a little hacky. If the token expires, every unit
            # will generate a new token, until the leader unit generates a new
            # valid token and sets it in the Juju secret.
            if self._juju_facade.is_leader:
                self._juju_facade.set_app_secret_content(
                    {"token": external_openbao.token},
                    label=self.AUTOUNSEAL_TOKEN_SECRET_LABEL,
                )
        return external_openbao.token
