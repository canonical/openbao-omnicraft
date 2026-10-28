#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import tempfile

import ops.testing as testing

from fixtures import OpenBaoCharmFixtures


class TestCharmInstall(OpenBaoCharmFixtures):
    def test_given_existing_data_exists_when_install_then_existing_data_is_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            openbao_raft_mount = testing.Mount(
                location="/openbao/raft",
                source=temp_dir,
            )
            container = testing.Container(
                name="openbao",
                can_connect=True,
                mounts={"openbao-raft": openbao_raft_mount},
            )
            state_in = testing.State(containers=[container])
            with open(f"{temp_dir}/vault.db", "w") as f:
                f.write("data")
            os.mkdir(f"{temp_dir}/raft")
            with open(f"{temp_dir}/raft/raft.db", "w") as f:
                f.write("data")

            self.ctx.run(self.ctx.on.install(), state_in)

            assert not os.path.exists(f"{temp_dir}/vault.db")
            assert not os.path.exists(f"{temp_dir}/raft/raft.db")
