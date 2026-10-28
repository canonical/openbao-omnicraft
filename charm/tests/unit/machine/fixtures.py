# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import contextlib
from unittest.mock import patch

import ops.testing as testing
import pytest
from charms.operator_libs_linux.v2 import snap
from openbao.openbao_managers import RaftManager
from openbao.testing.mocks import OpenBaoCharmFixturesBase

from charm import OpenBaoOperatorCharm


class OpenBaoCharmFixtures(OpenBaoCharmFixturesBase):
    @pytest.fixture(autouse=True)
    def setup(self):
        with (
            self.mocks(),  # common mocks from base class
            contextlib.ExitStack() as stack,
        ):
            # When we want to mock the instances, we use the return value of the mocked class
            self.mock_machine = stack.enter_context(patch("charm.Machine")).return_value
            self.mock_raft_manager = stack.enter_context(
                patch("charm.RaftManager", autospec=RaftManager)
            ).return_value
            # When we want to mock the callable, we use the mock directly
            self.mock_subprocess_run = stack.enter_context(
                patch("charm.subprocess.run", autospec=True)
            )
            self.mock_snap_cache = stack.enter_context(patch("charm.snap.SnapCache"))
            # Report the openbao snap as already installed by default so that
            # _install_openbao_snap does not try to fetch the snap resource.
            self.mock_snap_cache.return_value.__getitem__.return_value.state = (
                snap.SnapState.Present
            )
            self.mock_systemd_creds = stack.enter_context(patch("charm.SystemdCreds"))
            self.mock_logrotate_path = stack.enter_context(patch("charm.LOGROTATE_PATH"))
            yield

    @pytest.fixture(autouse=True)
    def context(self):
        self.ctx = testing.Context(charm_type=OpenBaoOperatorCharm)
