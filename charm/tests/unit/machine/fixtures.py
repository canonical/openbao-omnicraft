# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import contextlib
from unittest.mock import patch

import ops.testing as testing
import pytest

from charm import OpenBaoCharm
from openbao.openbao_managers import RaftManager
from openbao.testing.mocks import OpenBaoCharmFixturesBase
from workload_vm import OpenBaoVMWorkload


class OpenBaoCharmFixtures(OpenBaoCharmFixturesBase):
    @pytest.fixture(autouse=True)
    def setup(self):
        with (
            self.mocks(),  # common mocks from base class
            contextlib.ExitStack() as stack,
        ):
            # When we want to mock the instances, we use the return value of the mocked class
            self.mock_machine = stack.enter_context(
                patch("charm.OpenBaoVMWorkload", autospec=OpenBaoVMWorkload)
            ).return_value
            self.mock_machine.config_file_path = OpenBaoVMWorkload.config_file_path
            self.mock_machine.raft_storage_path = OpenBaoVMWorkload.raft_storage_path
            self.mock_machine.tls_directory_path = OpenBaoVMWorkload.tls_directory_path
            self.mock_machine.is_accessible.return_value = True
            self.mock_machine.configure_service.return_value = False
            self.mock_raft_manager = stack.enter_context(
                patch("charm.RaftManager", autospec=RaftManager)
            ).return_value
            yield

    @pytest.fixture(autouse=True)
    def context(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        self.ctx = testing.Context(charm_type=OpenBaoCharm)
