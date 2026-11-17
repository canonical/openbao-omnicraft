# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Library for managing OpenBao Charm features.

This library encapsulates the business logic for managing the OpenBao service and
its associated integrations within the context of our charms.

A OpenBao Feature Manager will aim to encapsulate as much of the business logic
related to the implementation of a specific feature as reasonably possible.

A feature, in this context, is any set of related concepts which distinctly
enhance the offering of the Charm by interacting with the OpenBao Service to
perform related operations. A feature may be optional, or required. Features
include TLS support, PKI and KV backends, and Auto-unseal.

Feature managers should:

- Abstract away any implementation specific details such as policy and mount
  names.
- Provide a simple interface for the charm to ensure the feature is correctly
  configured given the state of the charm. Ideally, this is a single method
  called `sync()`.
- Be idempotent.
- Be infrastructure independent (i.e. no Kubernetes or Machine specific code).
- Catch all expected exceptions, and prevent them from reaching the Charm.

Feature managers should not:

- Be concerned with the charm's lifecycle (i.e. Charm status)
- Depend on each other unless the features explicitly require the dependency.
"""
