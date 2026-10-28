#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import MagicMock

import ops.testing as testing
import pytest
from ops import ModelError
from ops.pebble import ChangeError, Layer, PathError

from core.base_workload import WorkloadError
from fixtures import OpenBaoCharmFixtures, k8s_only
from workload_k8s import OpenBaoK8sWorkload


@k8s_only
class TestCharmWithPebble(OpenBaoCharmFixtures):
    """Run the charm against the real Kubernetes workload and a Scenario container."""

    mock_workload_class = False

    def test_given_autounseal_token_when_pebble_ready_then_openbao_service_is_planned(
        self, tmp_path: Path
    ):
        self.set_openbao_ready()
        self.mock_openbao_autounseal_requirer_manager.get_provider_openbao_token.return_value = (
            "some token"
        )
        self.mock_autounseal_requires_get_details.return_value = MagicMock()
        container = testing.Container(
            name="openbao",
            can_connect=True,
            mounts={"config": testing.Mount(location="/openbao/config", source=tmp_path)},
        )

        state_out = self.ctx.run(
            self.ctx.on.pebble_ready(container), self.ready_state(containers=[container])
        )

        assert state_out.get_container("openbao").layers == {
            "openbao": Layer(
                {
                    "summary": "openbao layer",
                    "description": "pebble config layer for openbao",
                    "services": {
                        "openbao": {
                            "summary": "openbao",
                            "startup": "enabled",
                            "override": "replace",
                            "command": "bao server -config=/openbao/config/openbao.hcl",
                            "environment": {
                                "BAO_TOKEN": "some token",
                                "VAULT_TOKEN": "some token",
                            },
                        }
                    },
                }
            )
        }
        assert (tmp_path / "openbao.hcl").is_file()


@pytest.fixture
def container() -> MagicMock:
    container = MagicMock()
    container.get_plan.return_value.services = {}
    return container


def planned_environment(container: MagicMock) -> dict[str, str]:
    layer = container.add_layer.call_args.args[1]
    return layer.services["openbao"].environment


class TestOpenBaoK8sWorkload:
    def test_given_proxy_when_configure_service_then_cluster_traffic_bypasses_it(
        self, container: MagicMock
    ):
        changed = OpenBaoK8sWorkload(container).configure_service(
            None, {"HTTPS_PROXY": "http://proxy", "NO_PROXY": "10.0.0.0/8"}
        )

        assert changed is True
        assert planned_environment(container) == {
            "HTTPS_PROXY": "http://proxy",
            "NO_PROXY": "10.0.0.0/8,.svc.cluster.local",
        }

    def test_given_only_no_proxy_when_configure_service_then_it_is_kept_as_is(
        self, container: MagicMock
    ):
        OpenBaoK8sWorkload(container).configure_service(None, {"NO_PROXY": "10.0.0.0/8"})

        assert planned_environment(container) == {"NO_PROXY": "10.0.0.0/8"}

    def test_given_plan_unchanged_when_configure_service_then_no_layer_is_added(
        self, container: MagicMock
    ):
        workload = OpenBaoK8sWorkload(container)
        workload.configure_service("token", {})
        container.get_plan.return_value.services = container.add_layer.call_args.args[1].services
        container.add_layer.reset_mock()

        assert workload.configure_service("token", {}) is False
        container.add_layer.assert_not_called()

    def test_given_service_missing_when_is_running_then_false(self, container: MagicMock):
        container.get_service.side_effect = ModelError("no service")

        assert OpenBaoK8sWorkload(container).is_running() is False

    @pytest.mark.parametrize("operation, method", [("start", "replan"), ("stop", "stop")])
    def test_given_pebble_change_error_when_start_or_stop_then_workload_error_is_raised(
        self, container: MagicMock, operation: str, method: str
    ):
        getattr(container, method).side_effect = ChangeError("err", MagicMock())

        with pytest.raises(WorkloadError):
            getattr(OpenBaoK8sWorkload(container), operation)()

    def test_given_missing_path_when_remove_path_then_value_error_is_raised(
        self, container: MagicMock
    ):
        container.remove_path.side_effect = PathError("not-found", "no such file")

        with pytest.raises(ValueError):
            OpenBaoK8sWorkload(container).remove_path("/openbao/raft/vault.db")

    def test_when_hsm_operations_then_they_are_not_supported(self, container: MagicMock):
        workload = OpenBaoK8sWorkload(container)

        assert workload.install_hsm_lib(Path("/hsm-lib.tar.gz"), None) is None
        assert workload.pkcs11_kms_plugin() is None
