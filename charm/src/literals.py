# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Constants shared across the charm."""

from enum import Enum


class Substrate(str, Enum):
    """The kind of cloud the charm is deployed on."""

    K8S = "k8s"
    VM = "vm"
