# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures shared by the unit tests of the charm."""

import contextlib
from unittest.mock import MagicMock, patch

import ops.testing as testing
import pytest
from ops import ActiveStatus

from charm import OpenBaoCharm
from charms.data_platform_libs.v0.s3 import S3Requirer
from common.openbao_client import OpenBaoClient
from literals import Substrate
from managers.acme import ACMEManager
from managers.autounseal import AutounsealProviderManager, AutounsealRequirerManager
from managers.backup import BackupManager
from managers.kv import KVManager
from managers.pki import PKIManager
from managers.raft import RaftManager
from managers.tls import TLSManager
from workload_k8s import OpenBaoK8sWorkload
from workload_vm import OpenBaoVMWorkload

WORKLOADS = {Substrate.K8S: OpenBaoK8sWorkload, Substrate.VM: OpenBaoVMWorkload}

# Restrict a test of OpenBaoCharmFixtures to one substrate.
vm_only = pytest.mark.parametrize("setup", [Substrate.VM], indirect=True, ids=["vm"])
k8s_only = pytest.mark.parametrize("setup", [Substrate.K8S], indirect=True, ids=["k8s"])


def approle_secret() -> testing.Secret:
    """Return the secret the charm stores its AppRole in once authorized."""
    return testing.Secret(
        label="openbao-approle-auth-details",
        tracked_content={"role-id": "role id", "secret-id": "secret id"},
    )


def peer_network(address: str = "1.2.1.2") -> testing.Network:
    """Return the network of the peer relation, bound to the given address."""
    return testing.Network(
        "openbao-peers",
        bind_addresses=[testing.BindAddress([testing.Address(address)])],
        ingress_addresses=[address],
    )


class OpenBaoCharmFixtures:
    """Run each test once per substrate, with the workload and the feature managers mocked.

    `self.mock_workload` is an autospec of the workload class of the substrate,
    with its real paths. Restrict a test to a substrate with `vm_only` or `k8s_only`.
    """

    substrate: Substrate
    mock_workload_class = True
    mock_tls_class = True

    @pytest.fixture(autouse=True, params=[Substrate.VM, Substrate.K8S], ids=["vm", "k8s"])
    def setup(self, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
        self.substrate = request.param
        if self.substrate == Substrate.K8S:
            monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.152.183.1")
        else:
            monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        with contextlib.ExitStack() as stack:
            if self.mock_workload_class:
                self.mock_workload = self._mock_workload(stack, WORKLOADS[self.substrate])
            # When we want to mock the instances, we use the return value of the mocked class
            if self.mock_tls_class:
                self.mock_tls = self._patch(stack, "charm.TLSManager", TLSManager)
            self.mock_openbao = self._patch(stack, "charm.OpenBaoClient", OpenBaoClient)
            self.mock_openbao.generate_self_signed_ca.return_value = ("cert-pem", "key-pem")
            self.mock_openbao_autounseal_provider_manager = self._patch(
                stack, "charm.AutounsealProviderManager", AutounsealProviderManager
            )
            self.mock_openbao_autounseal_provider_manager.get_outstanding_requests.return_value = []
            self.mock_openbao_autounseal_requirer_manager = self._patch(
                stack, "charm.AutounsealRequirerManager", AutounsealRequirerManager
            )
            self.mock_kv_manager = self._patch(stack, "charm.KVManager", KVManager)
            self.mock_pki_manager = self._patch(stack, "charm.PKIManager", PKIManager)
            self.mock_acme_manager = self._patch(stack, "charm.ACMEManager", ACMEManager)
            self.mock_s3_requirer = self._patch(stack, "charm.S3Requirer", S3Requirer)
            self.mock_backup_manager = self._patch(
                stack, "events.actions.BackupManager", BackupManager
            )
            self.mock_raft_manager = self._patch(stack, "events.actions.RaftManager", RaftManager)
            # When we want to mock the callable, we use the mock directly
            self.mock_socket_fqdn = stack.enter_context(patch("socket.getfqdn"))
            self.mock_socket_fqdn.return_value = "myhostname"
            self.mock_get_requirer_assigned_certificate = stack.enter_context(
                patch("charm.TLSCertificatesRequiresV4.get_assigned_certificate")
            )
            self.mock_get_requirer_assigned_certificate.return_value = (None, None)
            stack.enter_context(patch("charm.TLSCertificatesRequiresV4.renew_certificate"))
            self.mock_pki_provider_get_outstanding_certificate_requests = stack.enter_context(
                patch("charm.TLSCertificatesProvidesV4.get_outstanding_certificate_requests")
            )
            stack.enter_context(patch("charm.TLSCertificatesProvidesV4.set_relation_certificate"))
            stack.enter_context(patch("charm.OpenBaoAutounsealProvides.set_autounseal_data"))
            self.mock_autounseal_requires_get_details = stack.enter_context(
                patch("charm.OpenBaoAutounsealRequires.get_details")
            )
            self.mock_autounseal_requires_get_details.return_value = None
            self.mock_kv_provides_get_credentials = stack.enter_context(
                patch("charm.VaultKvProvides.get_credentials")
            )
            self.mock_kv_provides_get_credentials.return_value = {}
            stack.enter_context(patch("charm.VaultKvProvides.set_kv_data"))
            # The library's recommended way of mocking the resources patch
            stack.enter_context(
                patch.multiple(
                    "charms.observability_libs.v0.kubernetes_compute_resources_patch.KubernetesComputeResourcesPatch",
                    _namespace="test-namespace",
                    _patch=lambda *a, **kw: None,
                    is_ready=lambda *a, **kw: True,
                    get_status=lambda _: ActiveStatus(),
                )
            )
            stack.enter_context(patch("lightkube.core.client.GenericSyncClient"))
            self.ctx = testing.Context(charm_type=OpenBaoCharm)
            yield

    @staticmethod
    def _patch(stack: contextlib.ExitStack, target: str, spec: type) -> MagicMock:
        """Patch a class with an autospec and return the mock of its instances."""
        return stack.enter_context(patch(target, autospec=spec)).return_value

    def _mock_workload(self, stack: contextlib.ExitStack, workload_class: type) -> MagicMock:
        """Mock a healthy, running workload that has the real paths of its substrate."""
        workload = self._patch(stack, f"charm.{workload_class.__name__}", workload_class)
        for path in ("config_file_path", "raft_storage_path", "tls_directory_path"):
            setattr(workload, path, getattr(workload_class, path))
        workload.is_accessible.return_value = True
        workload.is_running.return_value = True
        workload.exists.return_value = False
        workload.configure_service.return_value = False
        workload.install_hsm_lib.return_value = None
        workload.pkcs11_kms_plugin.return_value = None
        return workload

    def pushed_config(self) -> str:
        """Return the OpenBao config file the charm pushed to the workload."""
        pushed = [
            call.kwargs["source"]
            for call in self.mock_workload.push.call_args_list
            if call.kwargs.get("path") == self.mock_workload.config_file_path
        ]
        assert len(pushed) == 1, "the config file was not pushed exactly once"
        return pushed[0]

    def set_openbao_ready(self, **overrides: object) -> None:
        """Make the TLS files and a healthy, authorized OpenBao available, but for `overrides`.

        `overrides` map OpenBaoClient methods to their return value, or to an
        exception they raise.
        """
        self.mock_tls.configure_mock(
            **{
                "tls_file_pushed_to_workload.return_value": True,
                "tls_file_available_in_charm.return_value": True,
            }
        )
        returns: dict[str, object] = {
            "is_api_available": True,
            "is_initialized": True,
            "is_sealed": False,
            "needs_migration": False,
            "is_seal_type_transit": False,
            "get_seal_type": "shamir",
            "authenticate": True,
            "is_active": True,
            "is_active_or_standby": True,
            "is_raft_cluster_healthy": True,
            **overrides,
        }
        for method, value in returns.items():
            if isinstance(value, Exception):
                getattr(self.mock_openbao, method).side_effect = value
            else:
                getattr(self.mock_openbao, method).return_value = value

    def ready_state(self, *relations: testing.RelationBase, **kwargs: object) -> testing.State:
        """Return the state of an authorized leader unit with a bound peer network.

        `relations` are added to the peer relation, `kwargs` override the defaults.
        """
        defaults: dict[str, object] = {
            "leader": True,
            "relations": [testing.PeerRelation(endpoint="openbao-peers"), *relations],
            "secrets": [approle_secret()],
            "networks": [peer_network()],
        }
        return testing.State(**{**defaults, **kwargs})  # type: ignore[arg-type]
