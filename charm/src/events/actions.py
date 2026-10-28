# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Handlers of the charm actions."""

import json
import logging
from typing import TYPE_CHECKING

from ops import ActionEvent, Object

from common.exceptions import ManagerError
from common.juju_facade import NoSuchSecretError, SecretRemovedError
from common.openbao_client import OpenBaoAuthenticationError, OpenBaoClientError, Token
from literals import (
    APPROLE_ROLE_NAME,
    CHARM_POLICY_NAME,
    CHARM_POLICY_PATH,
    DEFAULT_POLICY_NAME,
    OPENBAO_CHARM_APPROLE_SECRET_LABEL,
    S3_RELATION_NAME,
)
from managers.backup import BackupManager
from managers.raft import RaftManager

if TYPE_CHECKING:
    from charm import OpenBaoCharm

logger = logging.getLogger(__name__)


class ActionEvents(Object):
    """Run the actions of the charm."""

    def __init__(self, charm: "OpenBaoCharm"):
        super().__init__(charm, "actions")
        self.charm = charm
        self.framework.observe(charm.on.authorize_charm_action, self._on_authorize_charm_action)
        self.framework.observe(charm.on.bootstrap_raft_action, self._on_bootstrap_raft_action)
        self.framework.observe(charm.on.create_backup_action, self._on_create_backup_action)
        self.framework.observe(charm.on.list_backups_action, self._on_list_backups_action)
        self.framework.observe(charm.on.restore_backup_action, self._on_restore_backup_action)

    def _on_authorize_charm_action(self, event: ActionEvent) -> None:
        """Grant the charm access to OpenBao with the token in the given secret."""
        if not self.charm.unit.is_leader():
            event.fail("This action must be run on the leader unit.")
            return
        try:
            token = self.charm.juju_facade.get_latest_secret_content(
                id=event.params.get("secret-id", "")
            ).get("token", "")
        except (NoSuchSecretError, SecretRemovedError):
            logger.warning(
                "Secret id provided could not be found by the charm when authorizing charm."
            )
            event.fail(
                "The secret id provided could not be found by the charm. Please grant the token secret to the charm."
            )
            return
        if not token:
            logger.warning("Token not found in the secret when authorizing charm.")
            event.fail("Token not found in the secret. Please provide a valid token secret.")
            return
        if not (openbao := self.charm.get_openbao_client()):
            event.fail("The OpenBao API address or CA certificate is not available yet.")
            return
        try:
            authenticated = openbao.authenticate(Token(token))
        except OpenBaoAuthenticationError:
            authenticated = False
        if not authenticated:
            logger.error("The token provided is not valid when authorizing charm.")
            event.fail(
                "The token provided is not valid. Please use a OpenBao token with the appropriate permissions."
            )
            return
        try:
            openbao.enable_approle_auth_method()
            openbao.create_or_update_policy_from_file(
                name=CHARM_POLICY_NAME, path=CHARM_POLICY_PATH
            )
            role_id = openbao.create_or_update_approle(
                name=APPROLE_ROLE_NAME,
                policies=[CHARM_POLICY_NAME, DEFAULT_POLICY_NAME],
                token_ttl="1h",
                token_max_ttl="1h",
            )
            secret_id = openbao.generate_role_secret_id(name=APPROLE_ROLE_NAME)
            self.charm.juju_facade.set_app_secret_content(
                content={"role-id": role_id, "secret-id": secret_id},
                label=OPENBAO_CHARM_APPROLE_SECRET_LABEL,
                description="The authentication details for the charm's access to openbao.",
            )
            event.set_results(
                {"result": "Charm authorized successfully. You may now remove the secret."}
            )
        except OpenBaoClientError as e:
            logger.exception("OpenBao returned an error while authorizing the charm")
            event.fail(f"OpenBao returned an error while authorizing the charm: {str(e)}")

    def _on_bootstrap_raft_action(self, event: ActionEvent) -> None:
        """Bootstraps the raft cluster when a single node is present.

        This is useful when OpenBao has lost quorum. The application must first
        be reduced to a single unit.
        """
        if not self.charm.api_address:
            event.fail(message="Network bind address is not available")
            return
        try:
            RaftManager(self.charm, self.charm.workload).bootstrap(
                self.charm.node_id, self.charm.api_address
            )
        except ManagerError as e:
            logger.error("Failed to bootstrap raft: %s", e)
            event.fail(message=f"Failed to bootstrap raft: {e}")
            return
        event.set_results({"result": "Raft cluster bootstrapped successfully."})

    def _on_create_backup_action(self, event: ActionEvent) -> None:
        """Create a snapshot, store it on S3 and return its ID."""
        if not (openbao_client := self.charm.get_authenticated_openbao_client()):
            event.fail(message="Failed to initialize OpenBao client.")
            return
        try:
            manager = BackupManager(self.charm, self.charm.s3_requirer, S3_RELATION_NAME)
            backup_key = manager.create_backup(
                openbao_client, skip_verify=event.params.get("skip-verify", False)
            )
        except ManagerError as e:
            logger.error("Failed to create backup: %s", e)
            event.fail(message=f"Failed to create backup: {e}")
            return
        event.set_results({"backup-id": backup_key})

    def _on_list_backups_action(self, event: ActionEvent) -> None:
        """List the IDs of the backups stored on S3."""
        try:
            manager = BackupManager(self.charm, self.charm.s3_requirer, S3_RELATION_NAME)
            backup_ids = manager.list_backups(skip_verify=event.params.get("skip-verify", False))
        except ManagerError as e:
            logger.error("Failed to list backups: %s", e)
            event.fail(message=f"Failed to list backups: {e}")
            return
        event.set_results({"backup-ids": json.dumps(backup_ids)})

    def _on_restore_backup_action(self, event: ActionEvent) -> None:
        """Restore the snapshot with the given ID."""
        if not (openbao_client := self.charm.get_active_openbao_client()):
            event.fail(message="Failed to initialize an active OpenBao client.")
            return
        key = event.params.get("backup-id")
        # This is enforced by Juju, the assert narrows the type.
        assert isinstance(key, str)
        try:
            manager = BackupManager(self.charm, self.charm.s3_requirer, S3_RELATION_NAME)
            manager.restore_backup(
                openbao_client, key, skip_verify=event.params.get("skip-verify", False)
            )
        except ManagerError as e:
            logger.error("Failed to restore backup: %s", e)
            event.fail(message=f"Failed to restore backup: {e}")
            return
        event.set_results({"restored": key})
