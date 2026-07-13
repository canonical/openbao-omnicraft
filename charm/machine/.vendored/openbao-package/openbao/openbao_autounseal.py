#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# Licensed under the Apache2.0. See LICENSE file in charm source for details.

"""Library for the openbao-autounseal relation.

This library contains the Requires and Provides classes for handling the
openbao-autounseal interface.

The provider side of the interface is responsible for enabling the openbao
transit engine and creating the necessary keys and policies for an external
openbao to be able to autounseal itself.

The requirer side of the interface is responsible for retrieving the necessary
details to autounseal the openbao instance, and configuring the openbao instance to
use them.

## Getting Started

### Provider charm

The provider charm is the charm that provides a OpenBao instance that can be
used to autounseal other OpenBao instances via the OpenBao transit backend.

Add the following to `metadata.yaml`:

```yaml
provides:
  openbao-autounseal-provides:
    interface: openbao-autounseal
```

### Requirer charm

The requirer charm is the charm that wishes to autounseal a OpenBao instance via
the OpenBao transit backend.

Add the following to `metadata.yaml`:

```yaml
requires:
  openbao-autounseal-requires:
    interface: openbao-autounseal
    limit: 1
```

### Integration

You can integrate both charms by running:

```bash
juju integrate <openbao a>:openbao-autounseal-provides <openbao b>:openbao-autounseal-requires
```

where `openbao a` is the OpenBao app which will provide the autounseal service, and
`openbao b` is the OpenBao app which will be configured for autounseal via `openbao a`.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, MutableMapping

from interface_tester import DataBagSchema
from ops import (
    CharmBase,
    EventBase,
    EventSource,
    Handle,
    Object,
    ObjectEvents,
    Relation,
    RelationBrokenEvent,
    RelationChangedEvent,
    RelationCreatedEvent,
    RelationDataContent,
)
from pydantic import BaseModel, Field, ValidationError

from openbao.juju_facade import (
    JujuFacade,
    MultipleRelationsFoundError,
    NoRemoteAppError,
    NoSuchRelationError,
    NoSuchSecretError,
    SecretRemovedError,
    TransientJujuError,
)

AUTOUNSEAL_CREDENTIALS_SECRET_LABEL_PREFIX = "openbao-autounseal-credentials-"


class LogAdapter(logging.LoggerAdapter):
    """Adapter for the logger to prepend a prefix to all log lines."""

    prefix = "openbao_autounseal"

    def process(self, msg: str, kwargs: MutableMapping) -> tuple[str, MutableMapping]:
        """Prepend the prefix to the log message."""
        return f"[{self.prefix}] {msg}", kwargs


logger = LogAdapter(logging.getLogger(__name__), {})


class OpenBaoAutounsealProviderSchema(BaseModel):
    """Provider side of the openbao-autounseal relation interface."""

    address: str = Field(description="The address of the OpenBao server to connect to.")
    mount_path: str = Field(
        description="The path to the transit engine mount point where the key is stored."
    )
    key_name: str = Field(description="The name of the transit key to use for autounseal.")
    credentials_secret_id: str = Field(
        description=(
            "The secret id of the Juju secret which stores the credentials for authenticating with the OpenBao server."
        )
    )
    ca_certificate: str = Field(
        description="The CA certificate to use when validating the OpenBao server's certificate."
    )


class ProviderSchema(DataBagSchema):
    """The schema for the provider side of this interface."""

    app: OpenBaoAutounsealProviderSchema  # pyright: ignore[reportIncompatibleVariableOverride, reportGeneralTypeIssues]


class OpenBaoAutounsealDetailsReadyEvent(EventBase):
    """Event emitted on the requirer when OpenBao autounseal details are ready in the databag."""

    def __init__(
        self,
        handle: Handle,
        address: str,
        mount_path: str,
        key_name: str,
        role_id: str,
        secret_id: str,
        ca_certificate: str,
    ):
        """OpenBaoAutounsealDetailsReadyEvent.

        Args:
            handle: ops.Handle
            address: The address of the OpenBao server to connect to.
            mount_path: The path to the transit engine mount point where the key is stored.
            key_name: The name of the transit key to use for autounseal.
            role_id: Approle role ID.
            secret_id: Approle secret ID.
            ca_certificate: The CA certificate to use when validating the OpenBao server's certificate.
        """
        super().__init__(handle)
        self.address = address
        self.mount_path = mount_path
        self.key_name = key_name
        self.role_id = role_id
        self.secret_id = secret_id
        self.ca_certificate = ca_certificate

    def snapshot(self) -> Dict[str, Any]:
        """Return snapshot data that should be persisted."""
        return dict(
            super().snapshot(),
            address=self.address,
            mount_path=self.mount_path,
            key_name=self.key_name,
            role_id=self.role_id,
            secret_id=self.secret_id,
            ca_certificate=self.ca_certificate,
        )

    def restore(self, snapshot: Dict[str, Any]) -> None:
        """Restore the event from a snapshot."""
        super().restore(snapshot)
        self.address = snapshot["address"]
        self.mount_path = snapshot["mount_path"]
        self.key_name = snapshot["key_name"]
        self.role_id = snapshot["role_id"]
        self.secret_id = snapshot["secret_id"]
        self.ca_certificate = snapshot["ca_certificate"]


class OpenBaoAutounsealProviderRemoved(EventBase):
    """Event emitted when the openbao that provided autounseal capabilities is removed."""


class OpenBaoAutounsealRequirerRelationCreated(EventBase):
    """Event emitted when OpenBao autounseal should be initialized for a new application."""

    def __init__(self, handle: Handle, relation: Relation):
        super().__init__(handle)
        self.relation = relation

    def snapshot(self) -> Dict[str, Any]:
        """Return snapshot data that should be persisted."""
        return dict(
            super().snapshot(),
            relation_id=self.relation.id,
            relation_name=self.relation.name,
        )

    def restore(self, snapshot: Dict[str, Any]) -> None:
        """Restore the event from a snapshot."""
        super().restore(snapshot)
        relation = self.framework.model.get_relation(
            snapshot["relation_name"], snapshot["relation_id"]
        )
        if relation is None:
            raise ValueError(
                f"Unable to restore {self}: relation {snapshot['relation_name']} (id={snapshot['relation_id']}) not found."
            )
        self.relation = relation


class OpenBaoAutounsealRequirerRelationBroken(EventBase):
    """Event emitted on the Provider when a relation to a Requirer is broken."""

    def __init__(self, handle: Handle, relation: Relation):
        super().__init__(handle)
        self.relation = relation

    def snapshot(self) -> Dict[str, Any]:
        """Return snapshot data that should be persisted."""
        return dict(
            super().snapshot(),
            relation_id=self.relation.id,
            relation_name=self.relation.name,
        )

    def restore(self, snapshot: Dict[str, Any]) -> None:
        """Restore the event from a snapshot."""
        super().restore(snapshot)
        relation = self.framework.model.get_relation(
            snapshot["relation_name"], snapshot["relation_id"]
        )
        if relation is None:
            raise ValueError(
                f"Unable to restore {self}: relation {snapshot['relation_name']} (id={snapshot['relation_id']}) not found."
            )
        self.relation = relation


class OpenBaoAutounsealProvidesEvents(ObjectEvents):
    """Events raised by the openbao-autounseal relation on the provider side."""

    openbao_autounseal_requirer_relation_created = EventSource(
        OpenBaoAutounsealRequirerRelationCreated
    )
    openbao_autounseal_requirer_relation_broken = EventSource(
        OpenBaoAutounsealRequirerRelationBroken
    )


class OpenBaoAutounsealRequireEvents(ObjectEvents):
    """Events raised by the openbao-autounseal relation on the requirer side."""

    openbao_autounseal_details_ready = EventSource(OpenBaoAutounsealDetailsReadyEvent)
    openbao_autounseal_provider_relation_broken = EventSource(OpenBaoAutounsealProviderRemoved)


@dataclass
class AutounsealDetails:
    """The details required to autounseal a openbao instance."""

    address: str
    mount_path: str
    key_name: str
    role_id: str
    secret_id: str
    ca_certificate: str


class OpenBaoAutounsealProvides(Object):
    """Manages the openbao-autounseal relation from the provider side."""

    on: OpenBaoAutounsealProvidesEvents = OpenBaoAutounsealProvidesEvents()  # type: ignore

    def __init__(self, charm: CharmBase, relation_name: str):
        super().__init__(charm, relation_name)
        self.charm = charm
        self.relation_name = relation_name
        self.juju_facade = JujuFacade(charm)

        self.framework.observe(
            self.charm.on[relation_name].relation_created, self._on_relation_created
        )
        self.framework.observe(
            self.charm.on[relation_name].relation_broken, self._on_relation_broken
        )

    def _on_relation_created(self, event: RelationCreatedEvent) -> None:
        """Handle the relation created event and emit a custom event."""
        self.on.openbao_autounseal_requirer_relation_created.emit(relation=event.relation)

    def _on_relation_broken(self, event: RelationBrokenEvent) -> None:
        """Handle the relation broken event and emit a custom event."""
        self.on.openbao_autounseal_requirer_relation_broken.emit(relation=event.relation)

    def set_autounseal_data(
        self,
        relation: Relation,
        openbao_address: str,
        mount_path: str,
        key_name: str,
        approle_role_id: str,
        approle_secret_id: str,
        ca_certificate: str,
    ) -> None:
        """Set the autounseal data in the relation databag.

        Args:
            relation: The Juju relation to set the autounseal data in.
            openbao_address: The address of the OpenBao server which will be used for autounseal
            mount_path: The path to the transit engine mount point where the key is stored.
            key_name: The name of the transit key to use for autounseal.
            approle_role_id: The AppRole Role ID to use when authenticating with the external OpenBao server.
            approle_secret_id: The AppRole Secret ID to use when authenticating with the external OpenBao server.
            ca_certificate: The CA certificate to use when validating the external OpenBao server's certificate.

        Raises:
            TransientJujuError
            SecretValidationError
            ValueError
        """
        if not self.juju_facade.is_leader:
            return
        secret = self.juju_facade.set_app_secret_content(
            label=f"{AUTOUNSEAL_CREDENTIALS_SECRET_LABEL_PREFIX}{relation.id}",
            content={
                "role-id": approle_role_id,
                "secret-id": approle_secret_id,
            },
        )
        self.juju_facade.grant_secret(secret=secret, relation=relation)
        if secret.id is None:
            raise ValueError("Secret id is None")
        self.juju_facade.set_app_relation_data(
            name=self.relation_name,
            id=relation.id,
            data={
                "address": openbao_address,
                "mount_path": mount_path,
                "key_name": key_name,
                "credentials_secret_id": secret.id,
                "ca_certificate": ca_certificate,
            },
        )


def _is_provider_data_valid(data: RelationDataContent) -> bool:
    """Use the pydantic schema to validate the data."""
    try:
        ProviderSchema(app=OpenBaoAutounsealProviderSchema(**data))
        return True
    except ValidationError as e:
        logger.warning("Invalid data: %s", e)
        return False


class OpenBaoAutounsealRequires(Object):
    """Manages the openbao-autounseal relation from the requirer side."""

    on: OpenBaoAutounsealRequireEvents = OpenBaoAutounsealRequireEvents()  # type: ignore

    def __init__(self, charm: CharmBase, relation_name: str):
        super().__init__(charm, relation_name)
        self.juju_facade = JujuFacade(charm)
        self.relation_name = relation_name

        self.framework.observe(charm.on[relation_name].relation_changed, self._on_relation_changed)
        self.framework.observe(charm.on[relation_name].relation_broken, self._on_relation_broken)

    def _on_relation_changed(self, event: RelationChangedEvent) -> None:
        data = event.relation.data[event.app]
        if _is_provider_data_valid(data):
            try:
                details = self.get_details()
            except TransientJujuError:
                return
            if not details:
                logger.warning("Missing details, but somehow we passed validation")
                return
            self.on.openbao_autounseal_details_ready.emit(
                details.address,
                details.mount_path,
                details.key_name,
                details.role_id,
                details.secret_id,
                details.ca_certificate,
            )

    def _on_relation_broken(self, _: RelationBrokenEvent) -> None:
        self.on.openbao_autounseal_provider_relation_broken.emit()

    def get_details(self) -> AutounsealDetails | None:
        """Return the openbao address, role id, secret id and ca certificate from the relation databag.

        Returns:
            An AutounsealDetails object if the data is valid, None otherwise.

        Raises:
            RuntimeError: If the requirer is related to more than one provider.
            TransientJujuError
        """
        try:
            relation_data = self.juju_facade.get_remote_app_relation_data(self.relation_name)
        except (NoSuchRelationError, NoRemoteAppError):
            return None
        except MultipleRelationsFoundError:
            raise RuntimeError("Autounseal requirer can't be related to more than one provider")
        address = relation_data.get("address")
        mount_path = relation_data.get("mount_path")
        key_name = relation_data.get("key_name")
        ca_certificate = relation_data.get("ca_certificate")
        credentials_secret_id = relation_data.get("credentials_secret_id")
        if not credentials_secret_id:
            return None
        try:
            role_id, secret_id = self.juju_facade.get_secret_content_values(
                "role-id",
                "secret-id",
                id=credentials_secret_id,
            )
        except (NoSuchSecretError, SecretRemovedError):
            return None
        if not (address and mount_path and key_name and ca_certificate and role_id and secret_id):
            return None
        return AutounsealDetails(
            address,
            mount_path,
            key_name,
            role_id,
            secret_id,
            ca_certificate,
        )
