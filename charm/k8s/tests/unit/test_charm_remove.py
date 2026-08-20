#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.


import os
import tempfile

import ops.testing as testing
from ops.pebble import Layer, ServiceStatus

from fixtures import OpenBaoCharmFixtures


class TestCharmRemove(OpenBaoCharmFixtures):
    def test_given_can_connect_when_remove_then_node_removed_from_raft_cluster_data_is_deleted(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.mock_openbao.configure_mock(
                **{
                    "is_api_available.return_value": True,
                    "is_initialized.return_value": True,
                    "is_sealed.return_value": False,
                    "is_node_in_raft_peers.return_value": True,
                    "get_num_raft_peers.return_value": 4,
                },
            )
            model_name = "model-name"
            approle_secret = testing.Secret(
                label="openbao-approle-auth-details",
                tracked_content={"role-id": "role id", "secret-id": "secret id"},
            )
            openbao_raft_mount = testing.Mount(
                location="/openbao/raft",
                source=temp_dir,
            )
            container = testing.Container(
                name="openbao",
                can_connect=True,
                mounts={"openbao-raft": openbao_raft_mount},
            )
            state_in = testing.State(
                containers=[container],
                secrets=[approle_secret],
                model=testing.Model(name=model_name),
            )
            with open(f"{temp_dir}/vault.db", "w") as f:
                f.write("data")
            os.mkdir(f"{temp_dir}/raft")
            with open(f"{temp_dir}/raft/raft.db", "w") as f:
                f.write("data")

            self.ctx.run(self.ctx.on.remove(), state_in)
            self.mock_openbao.remove_raft_node.assert_called_with(f"{model_name}-openbao-k8s/0")
            assert not os.path.exists(f"{temp_dir}/vault.db")
            assert not os.path.exists(f"{temp_dir}/raft/raft.db")

    def test_given_service_is_running_when_remove_then_service_is_stopped(self):
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_initialized.return_value": True,
                "is_node_in_raft_peers.return_value": True,
                "get_num_raft_peers.return_value": 4,
            },
        )
        approle_secret = testing.Secret(
            label="openbao-approle-auth-details",
            tracked_content={"role-id": "role id", "secret-id": "secret id"},
        )
        container = testing.Container(
            name="openbao",
            can_connect=True,
            layers={"openbao": Layer({"services": {"openbao": {}}})},
            service_statuses={"openbao": ServiceStatus.ACTIVE},
        )
        state_in = testing.State(
            containers=[container],
            secrets=[approle_secret],
        )

        state_out = self.ctx.run(self.ctx.on.remove(), state_in)
        assert list(state_out.containers)[0].service_statuses["openbao"] == ServiceStatus.INACTIVE
