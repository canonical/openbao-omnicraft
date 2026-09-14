#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


import ops.testing as testing
import pytest
from openbao.openbao_client import InitializationResult, OpenBaoClientError
from ops.testing import ActionFailed

from fixtures import OpenBaoCharmFixtures


class TestCharmInitializeAction(OpenBaoCharmFixtures):
    def test_given_unit_not_leader_when_initialize_then_fails(self):
        state_in = testing.State(leader=False)

        with pytest.raises(ActionFailed) as e:
            self.ctx.run(self.ctx.on.action("initialize"), state_in)
        assert e.value.message == "This action can only be run by the leader unit"

    def test_given_invalid_shares_when_initialize_then_fails(self):
        state_in = testing.State(leader=True)

        with pytest.raises(ActionFailed) as e:
            self.ctx.run(
                self.ctx.on.action(
                    "initialize", params={"secret-shares": 1, "secret-threshold": 2}
                ),
                state_in,
            )
        assert "secret-threshold" in e.value.message

    def test_given_already_initialized_when_initialize_then_fails(self):
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "initialize.side_effect": OpenBaoClientError("OpenBao is already initialized"),
            },
        )
        peer_relation = testing.PeerRelation(endpoint="openbao-peers")
        state_in = testing.State(
            leader=True,
            relations=[peer_relation],
            networks={
                testing.Network(
                    "openbao-peers",
                    bind_addresses=[testing.BindAddress([testing.Address("1.2.1.2")])],
                )
            },
        )

        with pytest.raises(ActionFailed) as e:
            self.ctx.run(self.ctx.on.action("initialize"), state_in)
        assert e.value.message == "OpenBao is already initialized"

    def test_given_uninitialized_when_initialize_then_stores_expiring_secret(self):
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "initialize.return_value": InitializationResult(
                    root_token="hvs.root", keys=["unseal-key"]
                ),
            },
        )
        peer_relation = testing.PeerRelation(endpoint="openbao-peers")
        state_in = testing.State(
            leader=True,
            relations=[peer_relation],
            networks={
                testing.Network(
                    "openbao-peers",
                    bind_addresses=[testing.BindAddress([testing.Address("1.2.1.2")])],
                )
            },
        )

        state_out = self.ctx.run(self.ctx.on.action("initialize"), state_in)

        self.mock_openbao.initialize.assert_called_once_with(secret_shares=1, secret_threshold=1)
        secret = state_out.get_secret(label="openbao-init-credentials")
        assert secret.tracked_content == {"token": "hvs.root", "key": "unseal-key"}
        assert self.ctx.action_results is not None
        assert self.ctx.action_results["secret-id"] == secret.id
        assert "expires" in self.ctx.action_results
        assert "token" not in self.ctx.action_results
        assert "key" not in self.ctx.action_results
        assert "unseal-key" not in self.ctx.action_results["result"]
        assert "hvs.root" not in self.ctx.action_results["result"]
