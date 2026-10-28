#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import json

import ops.testing as testing
import pytest

from common.exceptions import ManagerError
from common.openbao_client import OpenBaoAuthenticationError, OpenBaoClientError, Token
from fixtures import OpenBaoCharmFixtures, peer_network


class TestCharmAuthorizeAction(OpenBaoCharmFixtures):
    def authorize(self, state: testing.State, secret_id: str) -> testing.State:
        return self.ctx.run(
            self.ctx.on.action("authorize-charm", params={"secret-id": secret_id}), state
        )

    def assert_authorize_fails(self, state: testing.State, secret_id: str, message: str) -> None:
        with pytest.raises(testing.ActionFailed) as e:
            self.authorize(state, secret_id)
        assert e.value.message == message

    def test_given_unit_not_leader_when_authorize_charm_then_action_fails(self):
        self.assert_authorize_fails(
            self.ready_state(leader=False),
            "secret:abc",
            "This action must be run on the leader unit.",
        )

    def test_given_secret_id_not_found_when_authorize_charm_then_action_fails(self):
        self.assert_authorize_fails(
            self.ready_state(),
            "my secret id",
            "The secret id provided could not be found by the charm. Please grant the token secret to the charm.",
        )

    def test_given_no_token_in_secret_when_authorize_charm_then_action_fails(self):
        user_secret = testing.Secret(tracked_content={"not-token": "my token"})

        self.assert_authorize_fails(
            self.ready_state(secrets=[user_secret]),
            user_secret.id,
            "Token not found in the secret. Please provide a valid token secret.",
        )

    @pytest.mark.parametrize("unavailable", ["bind address", "CA certificate"])
    def test_given_openbao_unreachable_when_authorize_charm_then_action_fails(
        self, unavailable: str
    ):
        self.set_openbao_ready()
        user_secret = testing.Secret(tracked_content={"token": "my token"})
        state_in = self.ready_state(secrets=[user_secret])
        if unavailable == "bind address":
            state_in = self.ready_state(secrets=[user_secret], networks=[peer_network("")])
        else:
            self.mock_tls.tls_file_available_in_charm.return_value = False

        self.assert_authorize_fails(
            state_in,
            user_secret.id,
            "The OpenBao API address or CA certificate is not available yet.",
        )

    @pytest.mark.parametrize("authenticate", [False, OpenBaoAuthenticationError()])
    def test_given_invalid_token_when_authorize_charm_then_action_fails(
        self, authenticate: bool | Exception
    ):
        self.set_openbao_ready(authenticate=authenticate)
        user_secret = testing.Secret(tracked_content={"token": "invalid token"})

        self.assert_authorize_fails(
            self.ready_state(secrets=[user_secret]),
            user_secret.id,
            "The token provided is not valid. Please use a OpenBao token with the appropriate permissions.",
        )

    def test_given_openbao_client_error_when_authorize_charm_then_action_fails(self):
        self.set_openbao_ready()
        self.mock_openbao.enable_approle_auth_method.side_effect = OpenBaoClientError("boom")
        user_secret = testing.Secret(tracked_content={"token": "my token"})

        self.assert_authorize_fails(
            self.ready_state(secrets=[user_secret]),
            user_secret.id,
            "OpenBao returned an error while authorizing the charm: boom",
        )

    def test_given_valid_token_when_authorize_charm_then_charm_is_authorized(self):
        self.set_openbao_ready()
        self.mock_openbao.create_or_update_approle.return_value = "my-role-id"
        self.mock_openbao.generate_role_secret_id.return_value = "my-secret-id"
        user_secret = testing.Secret(tracked_content={"token": "my token"})

        state_out = self.authorize(self.ready_state(secrets=[user_secret]), user_secret.id)

        assert self.mock_openbao.authenticate.call_args_list[0].args == (Token("my token"),)
        self.mock_openbao.enable_approle_auth_method.assert_called_once()
        self.mock_openbao.create_or_update_policy_from_file.assert_called_once_with(
            name="charm-access", path="src/templates/charm_policy.hcl"
        )
        self.mock_openbao.create_or_update_approle.assert_called_once_with(
            name="charm", policies=["charm-access", "default"], token_ttl="1h", token_max_ttl="1h"
        )
        assert self.ctx.action_results == {
            "result": "Charm authorized successfully. You may now remove the secret."
        }
        assert state_out.get_secret(label="openbao-approle-auth-details").tracked_content == {
            "role-id": "my-role-id",
            "secret-id": "my-secret-id",
        }


class TestCharmBootstrapRaftAction(OpenBaoCharmFixtures):
    def test_given_no_bind_address_when_bootstrap_raft_then_action_fails(self):
        with pytest.raises(testing.ActionFailed) as e:
            self.ctx.run(
                self.ctx.on.action("bootstrap-raft"),
                self.ready_state(networks=[peer_network("")]),
            )
        assert e.value.message == "Network bind address is not available"

    def test_given_manager_error_when_bootstrap_raft_then_action_fails(self):
        self.mock_raft_manager.bootstrap.side_effect = ManagerError("some error message")

        with pytest.raises(testing.ActionFailed) as e:
            self.ctx.run(self.ctx.on.action("bootstrap-raft"), self.ready_state())
        assert e.value.message == "Failed to bootstrap raft: some error message"

    def test_given_single_unit_when_bootstrap_raft_then_node_is_bootstrapped(self):
        self.ctx.run(
            self.ctx.on.action("bootstrap-raft"),
            self.ready_state(model=testing.Model(name="model")),
        )

        host = "myhostname" if self.substrate.value == "k8s" else "1.2.1.2"
        self.mock_raft_manager.bootstrap.assert_called_once_with(
            "model-openbao/0", f"https://{host}:8200"
        )
        assert self.ctx.action_results == {"result": "Raft cluster bootstrapped successfully."}


class TestCharmBackupActions(OpenBaoCharmFixtures):
    def run_action(self, action: str, **params: object) -> testing.State:
        return self.ctx.run(self.ctx.on.action(action, params=params), self.ready_state())

    def assert_action_fails(self, action: str, message: str, **params: object) -> None:
        with pytest.raises(testing.ActionFailed) as e:
            self.run_action(action, **params)
        assert e.value.message == message

    def test_given_openbao_unavailable_when_create_backup_then_action_fails(self):
        self.set_openbao_ready(is_api_available=False)

        self.assert_action_fails("create-backup", "Failed to initialize OpenBao client.")

    def test_given_manager_error_when_create_backup_then_action_fails(self):
        self.set_openbao_ready()
        self.mock_backup_manager.create_backup.side_effect = ManagerError("some error message")

        self.assert_action_fails("create-backup", "Failed to create backup: some error message")

    def test_when_create_backup_then_backup_id_is_returned(self):
        self.set_openbao_ready()
        self.mock_backup_manager.create_backup.return_value = "backup-key"

        self.run_action("create-backup", **{"skip-verify": True})

        self.mock_backup_manager.create_backup.assert_called_once_with(
            self.mock_openbao, skip_verify=True
        )
        assert self.ctx.action_results == {"backup-id": "backup-key"}

    def test_given_manager_error_when_list_backups_then_action_fails(self):
        self.mock_backup_manager.list_backups.side_effect = ManagerError("some error message")

        self.assert_action_fails("list-backups", "Failed to list backups: some error message")

    def test_when_list_backups_then_backup_ids_are_returned(self):
        self.mock_backup_manager.list_backups.return_value = ["backup-1", "backup-2"]

        self.run_action("list-backups")

        assert self.ctx.action_results == {"backup-ids": json.dumps(["backup-1", "backup-2"])}

    def test_given_no_active_node_when_restore_backup_then_action_fails(self):
        self.set_openbao_ready(is_active=False)

        self.assert_action_fails(
            "restore-backup",
            "Failed to initialize an active OpenBao client.",
            **{"backup-id": "backup-key"},
        )

    def test_given_manager_error_when_restore_backup_then_action_fails(self):
        self.set_openbao_ready()
        self.mock_backup_manager.restore_backup.side_effect = ManagerError("some error message")

        self.assert_action_fails(
            "restore-backup",
            "Failed to restore backup: some error message",
            **{"backup-id": "backup-key"},
        )

    def test_when_restore_backup_then_backup_is_restored(self):
        self.set_openbao_ready()

        self.run_action("restore-backup", **{"backup-id": "backup-key"})

        self.mock_backup_manager.restore_backup.assert_called_once_with(
            self.mock_openbao, "backup-key", skip_verify=False
        )
        assert self.ctx.action_results == {"restored": "backup-key"}
