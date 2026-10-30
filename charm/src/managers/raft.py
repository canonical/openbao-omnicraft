# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Bootstrap the raft cluster."""

import json
import logging

from ops import CharmBase

from common.exceptions import ManagerError
from common.juju_facade import (
    JujuFacade,
)
from core.base_workload import WorkloadBase

logger = logging.getLogger(__name__)


class RaftManager:
    """Encapsulates the business logic for managing the bootstrap of a OpenBao cluster in Raft mode."""

    def __init__(
        self,
        charm: CharmBase,
        workload: WorkloadBase,
    ):
        self._juju_facade = JujuFacade(charm)
        self._workload = workload

    def bootstrap(self, node_id: str, address: str) -> None:
        """Bootstrap a OpenBao cluster in Raft mode.

        This method will bootstrap a OpenBao cluster for a single node, by
        identifying itself as the sole node in the cluster. Additional units
        may then be added once the cluster is available.
        """
        if not self._juju_facade.is_leader:
            logger.debug("Only leader unit can bootstrap a OpenBao cluster")
            raise ManagerError("Only the leader unit can bootstrap a OpenBao cluster")
        if not self._juju_facade.planned_units_for_app == 1:
            raise ManagerError("Bootstrapping a OpenBao cluster requires exactly one unit")

        self._workload.stop()
        self._push_peers_json(node_id, address)
        self._workload.restart()

        logger.info("OpenBao cluster bootstrapped in Raft mode")

    def _push_peers_json(self, node_id: str, address: str) -> None:
        """Create the peers.json file for the OpenBao cluster.

        This method will create the peers.json file for the OpenBao cluster based
        on the ``node_id`` and ``address`` provided.
        """
        self._workload.push(
            f"{self._workload.raft_storage_path}/raft/peers.json",
            self._get_peers_json(node_id, address),
        )

    def _get_peers_json(self, node_id: str, address: str) -> str:
        """Return the peers.json file content for bootstrapping raft.

        This method will return the content of the peers.json file based on the
        ``node_id`` and ``address`` provided.
        """
        return json.dumps([{"id": node_id, "address": address}])
