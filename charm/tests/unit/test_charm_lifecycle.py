#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import call

import ops.testing as testing

from core.base_workload import WorkloadError
from fixtures import OpenBaoCharmFixtures, k8s_only, vm_only


class TestCharmInstall(OpenBaoCharmFixtures):
    @k8s_only
    def test_given_reused_volume_when_install_then_raft_data_is_deleted(self):
        self.ctx.run(self.ctx.on.install(), testing.State())

        self.mock_workload.remove_path.assert_has_calls(
            [call(path="/openbao/raft/vault.db"), call(path="/openbao/raft/raft/raft.db")]
        )

    @k8s_only
    def test_given_container_not_ready_when_install_then_event_is_deferred(self):
        self.mock_workload.is_accessible.return_value = False

        state_out = self.ctx.run(self.ctx.on.install(), testing.State())

        assert [event.name for event in state_out.deferred] == ["install"]
        self.mock_workload.remove_path.assert_not_called()

    @vm_only
    def test_given_machine_when_install_then_snap_is_installed_and_data_is_kept(self):
        self.ctx.run(self.ctx.on.install(), testing.State())

        self.mock_workload.install.assert_called_once()
        self.mock_workload.remove_path.assert_not_called()

    def test_given_install_fails_when_configure_then_openbao_is_not_configured(self):
        self.mock_workload.install.side_effect = WorkloadError("no snapd")

        self.ctx.run(self.ctx.on.config_changed(), self.ready_state())

        self.mock_workload.push.assert_not_called()
        self.mock_workload.start.assert_not_called()


class TestCharmRemove(OpenBaoCharmFixtures):
    def remove(self) -> None:
        self.ctx.run(self.ctx.on.remove(), self.ready_state(model=testing.Model(name="model")))

    def test_given_cluster_when_remove_then_node_leaves_the_cluster_and_data_is_deleted(self):
        self.set_openbao_ready(is_node_in_raft_peers=True, get_num_raft_peers=3)

        self.remove()

        self.mock_openbao.remove_raft_node.assert_called_once_with(id="model-openbao/0")
        raft = self.mock_workload.raft_storage_path
        self.mock_workload.remove_path.assert_has_calls(
            [call(path=f"{raft}/vault.db"), call(path=f"{raft}/raft/raft.db")]
        )

    def test_given_last_node_when_remove_then_node_is_not_removed_from_the_cluster(self):
        self.set_openbao_ready(is_node_in_raft_peers=True, get_num_raft_peers=1)

        self.remove()

        self.mock_openbao.remove_raft_node.assert_not_called()

    def test_given_openbao_sealed_when_remove_then_data_is_still_deleted(self):
        self.set_openbao_ready(is_sealed=True)

        self.remove()

        self.mock_openbao.remove_raft_node.assert_not_called()
        self.mock_workload.stop.assert_called_once_with()
        assert self.mock_workload.remove_path.call_count == 2

    def test_given_service_not_running_when_remove_then_service_is_not_stopped(self):
        self.mock_workload.is_running.return_value = False

        self.remove()

        self.mock_workload.stop.assert_not_called()

    def test_given_stop_fails_when_remove_then_data_is_still_deleted(self):
        self.mock_workload.stop.side_effect = WorkloadError("stuck")

        self.remove()

        assert self.mock_workload.remove_path.call_count == 2

    @k8s_only
    def test_given_container_not_ready_when_remove_then_nothing_is_done(self):
        self.mock_workload.is_accessible.return_value = False

        self.remove()

        self.mock_openbao.remove_raft_node.assert_not_called()
        self.mock_workload.remove_path.assert_not_called()
