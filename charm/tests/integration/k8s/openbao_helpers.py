#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""OpenBao helper functions."""

import logging
import time
from os.path import abspath
from typing import Tuple

import hvac
import requests
from hvac.exceptions import InvalidRequest

logger = logging.getLogger(__name__)

# OpenBao status codes, see
# https://openbao.org/api-docs/system/health for more details
OPENBAO_STATUS_ACTIVE = 200
OPENBAO_STATUS_NOT_INITIALIZED = 501


class OpenBao:
    def __init__(self, url: str, ca_file_location: str | None = None, token: str | None = None):
        self.url = url
        verify = abspath(ca_file_location) if ca_file_location else False
        self.client = hvac.Client(url=self.url, verify=verify)
        if token:
            self.client.token = token

    def initialize(self) -> Tuple[str, str]:
        """Initialize the openbao unit and return the root token and unseal key."""
        seal_type = self.client.seal_status["type"]  # type: ignore -- bad type hints in stubs
        if seal_type == "shamir":
            initialize_response = self.client.sys.initialize(secret_shares=1, secret_threshold=1)
            root_token, unseal_key = (
                initialize_response["root_token"],
                initialize_response["keys"][0],
            )
            return root_token, unseal_key
        initialize_response = self.client.sys.initialize(recovery_shares=1, recovery_threshold=1)
        root_token, recovery_key = (
            initialize_response["root_token"],
            initialize_response["recovery_keys"][0],
        )
        return root_token, recovery_key

    def is_initialized(self) -> bool:
        """Check if the openbao unit is initialized."""
        response = self.client.sys.read_health_status()
        return response.status_code != OPENBAO_STATUS_NOT_INITIALIZED

    def is_sealed(self) -> bool:
        """Check if the openbao unit is sealed."""
        return self.client.sys.is_sealed()

    def is_active(self) -> bool:
        """Check if the openbao unit is active."""
        response = self.client.sys.read_health_status()
        return response.status_code == OPENBAO_STATUS_ACTIVE

    def wait_for_node_to_be_unsealed(self) -> None:
        """Wait for the openbao unit to be unsealed."""
        timeout = 300
        t0 = time.time()
        while time.time() < t0 + timeout:
            time.sleep(5)
            try:
                if not self.is_sealed():
                    logger.info("OpenBao unit is unsealed.")
                    return
            except requests.exceptions.ConnectionError:
                logger.debug("OpenBao is not yet available. Waiting...")
                continue
        raise TimeoutError("Timed out waiting for openbao to be unsealed.")

    def unseal(self, unseal_key: str) -> None:
        """Unseal a openbao unit."""
        timeout = 300
        t0 = time.time()
        while time.time() < t0 + timeout:
            try:
                if not self.client.sys.is_sealed():
                    return
                self.client.sys.submit_unseal_key(unseal_key)
                logger.info("Unsealed openbao unit: %s.", self.url)
                return
            except requests.exceptions.RequestException:
                logger.debug("OpenBao is not yet available. Waiting...")
                time.sleep(5)
            except InvalidRequest as e:
                # A raft follower cannot accept an unseal key until it has
                # joined the cluster, which may take a few retry_join cycles
                # after the leader is unsealed.
                if "not initialized" not in str(e).lower():
                    raise
                logger.debug("Node not ready to unseal yet. Waiting...")
                time.sleep(5)
        raise TimeoutError("Timed out unsealing openbao unit.")

    def wait_for_raft_nodes(self, expected_num_nodes: int) -> None:
        """Wait for the specified number of units to join the raft cluster."""
        timeout = 300
        t0 = time.time()
        while time.time() < t0 + timeout:
            time.sleep(5)
            response = self.client.sys.read_raft_config()
            servers = response["data"]["config"]["servers"]
            current_num_voters = sum(1 for server in servers if server.get("voter", False))
            current_num_nodes = len(servers)
            if current_num_nodes != expected_num_nodes:
                logger.info(
                    "Nodes in the raft cluster: %d/%d",
                    current_num_nodes,
                    expected_num_nodes,
                )
                continue
            if current_num_voters != expected_num_nodes:
                logger.info(
                    "Voters in the raft cluster: %d/%d", current_num_voters, current_num_nodes
                )
                continue
            logger.info(
                "Expected number of nodes are part of the raft cluster: %d/%d",
                current_num_nodes,
                expected_num_nodes,
            )
            return
        raise TimeoutError("Timed out waiting for nodes to be part of the raft cluster.")

    def number_of_raft_nodes(self) -> int:
        """Get the number of nodes in the raft cluster."""
        response = self.client.sys.read_raft_config()
        servers = response["data"]["config"]["servers"]
        return len(servers)
