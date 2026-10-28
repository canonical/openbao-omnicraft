# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import contextlib
from unittest.mock import patch

import ops.testing as testing
import pytest
from ops.model import ActiveStatus
from ops.pebble import Layer, ServiceStatus

from charm import OpenBaoCharm
from openbao.testing.mocks import OpenBaoCharmFixturesBase


class OpenBaoCharmFixtures(OpenBaoCharmFixturesBase):
    @pytest.fixture(autouse=True)
    def setup(self):
        with (
            self.mocks(),  # common mocks from base class
            contextlib.ExitStack() as stack,
        ):
            # When we want to mock the instances, we use the return value of the mocked class
            # When we want to mock the callable, we use the mock directly
            self.mock_get_binding = stack.enter_context(patch("ops.model.Model.get_binding"))

            # Mock KubernetesComputeResourcesPatch using the library's recommended approach
            stack.enter_context(
                patch.multiple(
                    "charms.observability_libs.v0.kubernetes_compute_resources_patch.KubernetesComputeResourcesPatch",
                    _namespace="test-namespace",
                    is_ready=lambda *a, **kw: True,
                    get_status=lambda _: ActiveStatus(),
                )
            )
            stack.enter_context(patch("lightkube.core.client.GenericSyncClient"))
            yield

    @pytest.fixture(autouse=True)
    def context(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.152.183.1")
        self.ctx = testing.Context(charm_type=OpenBaoCharm)


class MockNetwork:
    def __init__(self, bind_address: str, ingress_address: str):
        self.bind_address = bind_address
        self.ingress_address = ingress_address
        self.ingress_addresses = [ingress_address]


class MockBinding:
    def __init__(self, bind_address: str, ingress_address: str):
        self.network = MockNetwork(bind_address=bind_address, ingress_address=ingress_address)


def running_container() -> testing.Container:
    """Return the openbao container with its service running."""
    return testing.Container(
        name="openbao",
        can_connect=True,
        layers={
            "openbao": Layer({"services": {"openbao": {"override": "replace", "command": "bao"}}})
        },
        service_statuses={"openbao": ServiceStatus.ACTIVE},
    )
