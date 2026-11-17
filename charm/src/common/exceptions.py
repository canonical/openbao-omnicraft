# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Exceptions shared across the charm."""


class ManagerError(Exception):
    """Exception raised when a manager encounters an error."""

    pass


class OpenBaoCertsError(Exception):
    """Exception raised when a openbao certificate is not found."""

    def __init__(
        self, message: str = "Could not retrieve openbao certificates from local storage"
    ):
        self.message = message
        super().__init__(self.message)
