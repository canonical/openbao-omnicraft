#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import ops.testing as testing
import pytest
from openbao.openbao_client import OpenBaoClientError

from fixtures import OpenBaoCharmFixtures


class TestCharmUnsealAction(OpenBaoCharmFixtures):
    def _state(self, secrets=None):
        container = testing.Container(name="openbao", can_connect=True)
        peer_relation = testing.PeerRelation(endpoint="openbao-peers")
        return testing.State(
            containers=[container],
            leader=True,
            secrets=secrets or [],
            relations=[peer_relation],
        )

    def test_given_secret_id_not_found_when_unseal_then_fails(self):
        with pytest.raises(testing.ActionFailed) as exc:
            self.ctx.run(
                self.ctx.on.action("unseal", params={"secret-id": "missing"}),
                state=self._state(),
            )
        assert "could not be found by the charm" in exc.value.message

    def test_given_key_missing_from_secret_when_unseal_then_fails(self):
        secret = testing.Secret(tracked_content={"token": "hvs.root"})
        with pytest.raises(testing.ActionFailed) as exc:
            self.ctx.run(
                self.ctx.on.action("unseal", params={"secret-id": secret.id}),
                state=self._state(secrets=[secret]),
            )
        assert "Unseal key not found" in exc.value.message

    def test_given_not_initialized_when_unseal_then_fails(self):
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": False,
            },
        )
        secret = testing.Secret(tracked_content={"key": "unseal-key"})
        with pytest.raises(testing.ActionFailed) as exc:
            self.ctx.run(
                self.ctx.on.action("unseal", params={"secret-id": secret.id}),
                state=self._state(secrets=[secret]),
            )
        assert "not initialized" in exc.value.message

    def test_given_auto_unseal_when_unseal_then_fails(self):
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "needs_migration.return_value": False,
                "get_seal_type.return_value": "transit",
            },
        )
        secret = testing.Secret(tracked_content={"key": "recovery-key"})
        with pytest.raises(testing.ActionFailed) as exc:
            self.ctx.run(
                self.ctx.on.action("unseal", params={"secret-id": secret.id}),
                state=self._state(secrets=[secret]),
            )
        assert "auto-unseal" in exc.value.message

    def test_given_invalid_key_when_unseal_then_fails_without_key_in_message(self):
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "needs_migration.return_value": False,
                "get_seal_type.return_value": "shamir",
                "is_sealed.return_value": True,
                "unseal.side_effect": OpenBaoClientError("Failed to unseal OpenBao"),
            },
        )
        secret = testing.Secret(tracked_content={"key": "super-secret-key"})
        with pytest.raises(testing.ActionFailed) as exc:
            self.ctx.run(
                self.ctx.on.action("unseal", params={"secret-id": secret.id}),
                state=self._state(secrets=[secret]),
            )
        assert exc.value.message == "Failed to unseal OpenBao"
        assert "super-secret-key" not in exc.value.message

    def test_given_already_unsealed_when_unseal_then_succeeds(self):
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "needs_migration.return_value": False,
                "get_seal_type.return_value": "shamir",
                "is_sealed.return_value": False,
            },
        )
        secret = testing.Secret(tracked_content={"key": "unseal-key"})
        self.ctx.run(
            self.ctx.on.action("unseal", params={"secret-id": secret.id}),
            state=self._state(secrets=[secret]),
        )
        self.mock_openbao.unseal.assert_not_called()
        assert self.ctx.action_results == {
            "sealed": False,
            "progress": 0,
            "threshold": 0,
            "result": "OpenBao is already unsealed.",
        }

    def test_given_sealed_when_unseal_then_succeeds(self):
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "needs_migration.return_value": False,
                "get_seal_type.return_value": "shamir",
                "is_sealed.return_value": True,
                "unseal.return_value": {"sealed": False, "progress": 1, "t": 1},
            },
        )
        secret = testing.Secret(tracked_content={"key": "unseal-key"})
        self.ctx.run(
            self.ctx.on.action("unseal", params={"secret-id": secret.id}),
            state=self._state(secrets=[secret]),
        )
        self.mock_openbao.unseal.assert_called_once_with("unseal-key")
        assert self.ctx.action_results == {
            "sealed": False,
            "progress": 1,
            "threshold": 1,
            "result": "OpenBao unsealed.",
        }
