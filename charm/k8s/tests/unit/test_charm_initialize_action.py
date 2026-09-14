#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import ops.testing as testing
import pytest
from openbao.openbao_client import InitializationResult, OpenBaoClientError

from fixtures import OpenBaoCharmFixtures


class TestCharmInitializeAction(OpenBaoCharmFixtures):
    def _container_state(self, leader=True, secrets=None):
        container = testing.Container(name="openbao", can_connect=True)
        return testing.State(
            containers=[container],
            leader=leader,
            secrets=secrets or [],
        )

    def test_given_unit_not_leader_when_initialize_then_fails(self):
        with pytest.raises(testing.ActionFailed) as exc:
            self.ctx.run(
                self.ctx.on.action("initialize"),
                state=self._container_state(leader=False),
            )
        assert "This action must be run on the leader unit." in exc.value.message

    def test_given_invalid_shares_when_initialize_then_fails(self):
        with pytest.raises(testing.ActionFailed) as exc:
            self.ctx.run(
                self.ctx.on.action(
                    "initialize", params={"secret-shares": 1, "secret-threshold": 2}
                ),
                state=self._container_state(),
            )
        assert "secret-threshold" in exc.value.message

    def test_given_already_initialized_when_initialize_then_fails(self):
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "initialize.side_effect": OpenBaoClientError("OpenBao is already initialized"),
            },
        )
        with pytest.raises(testing.ActionFailed) as exc:
            self.ctx.run(self.ctx.on.action("initialize"), state=self._container_state())
        assert exc.value.message == "OpenBao is already initialized"

    def test_given_uninitialized_when_initialize_then_stores_expiring_secret(self):
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "initialize.return_value": InitializationResult(
                    root_token="hvs.root", keys=["unseal-key"]
                ),
            },
        )
        state_out = self.ctx.run(self.ctx.on.action("initialize"), state=self._container_state())
        self.mock_openbao.initialize.assert_called_once_with(secret_shares=1, secret_threshold=1)
        secret = state_out.get_secret(label="openbao-init-credentials")
        assert secret.tracked_content == {"token": "hvs.root", "key": "unseal-key"}
        assert self.ctx.action_results is not None
        assert self.ctx.action_results["secret-id"] == secret.id
        assert "expires" in self.ctx.action_results
        assert "token" not in self.ctx.action_results
        assert "key" not in self.ctx.action_results
