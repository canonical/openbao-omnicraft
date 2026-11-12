# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Back up and restore raft snapshots on S3."""

import logging

from ops import CharmBase

from charms.data_platform_libs.v0.s3 import S3Requirer
from common.exceptions import ManagerError
from common.juju_facade import (
    JujuFacade,
)
from common.openbao_client import (
    OpenBaoClient,
    OpenBaoClientError,
)
from common.openbao_s3 import S3, S3Error
from literals import Naming

logger = logging.getLogger(__name__)


class BackupManager:
    """Encapsulates the business logic for managing backups in OpenBao from a Charm.

    This class provides the business logic for creating, listing, and restoring
    backups of the OpenBao data.
    """

    REQUIRED_S3_PARAMETERS = ["bucket", "access-key", "secret-key", "endpoint"]

    def __init__(
        self,
        charm: CharmBase,
        s3_requirer: S3Requirer,
        relation_name: str,
    ):
        self._charm = charm
        self._juju_facade = JujuFacade(charm)
        self._s3_requirer = s3_requirer
        self._relation_name = relation_name

    def create_backup(self, openbao_client: OpenBaoClient, skip_verify: bool = False) -> str:
        """Create a backup of the OpenBao data.

        Stores the backup in the S3 bucket provided by the S3 relation.

        Args:
            openbao_client: The OpenBao client to use for creating the snapshot
            skip_verify: Whether to skip verification of the S3 connection.

        Returns:
            The S3 key of the backup.
        """
        self._validate_s3_prerequisites()

        s3_parameters = self._get_s3_parameters()

        try:
            s3 = S3(
                access_key=s3_parameters["access-key"],
                secret_key=s3_parameters["secret-key"],
                endpoint=s3_parameters["endpoint"],
                region=s3_parameters.get("region"),
                skip_verify=skip_verify,
                application=self._charm.app.name,
            )
        except S3Error as e:
            logger.error("Failed to create S3 session. %s", e)
            raise ManagerError("Failed to create S3 session")

        if not (s3.create_bucket(bucket_name=s3_parameters["bucket"])):
            raise ManagerError("Failed to create S3 bucket")
        backup_key = Naming.backup_s3_key_name(self._charm.model.name)

        response = openbao_client.create_snapshot()
        content_uploaded = s3.upload_content(
            content=response.raw,  # type: ignore[reportArgumentType]
            bucket_name=s3_parameters["bucket"],
            key=backup_key,
        )
        if not content_uploaded:
            raise ManagerError("Failed to upload backup to S3 bucket")
        logger.info("Backup uploaded to S3 bucket %s", s3_parameters["bucket"])
        return backup_key

    def list_backups(self, skip_verify: bool = False) -> list[str]:
        """List all the backups available in the S3 bucket.

        Backups are identified by the key prefix from
        ``Naming.backup_s3_key_prefix``.

        Args:
            skip_verify: Whether to skip verification of the S3 connection.

        Returns:
            A list of backup keys with the prefix.
        """
        self._validate_s3_prerequisites()

        s3_parameters = self._get_s3_parameters()

        try:
            s3 = S3(
                access_key=s3_parameters["access-key"],
                secret_key=s3_parameters["secret-key"],
                endpoint=s3_parameters["endpoint"],
                region=s3_parameters.get("region"),
                skip_verify=skip_verify,
                application=self._charm.app.name,
            )
        except S3Error:
            raise ManagerError("Failed to create S3 session")

        try:
            backup_ids = s3.get_object_key_list(
                bucket_name=s3_parameters["bucket"], prefix=Naming.backup_s3_key_prefix
            )
        except S3Error as e:
            raise ManagerError(f"Failed to list backups in S3 bucket: {e}")
        return backup_ids

    def restore_backup(
        self, openbao_client: OpenBaoClient, backup_key: str, skip_verify: bool = False
    ) -> None:
        """Restore the OpenBao data from the backup using the ``openbao_client`` provided.

        Args:
            openbao_client: The OpenBao client to use for restoring the snapshot
            backup_key: The S3 key of the backup to restore
            skip_verify: Whether to skip verification of the S3 connection.
        """
        self._validate_s3_prerequisites()

        s3_parameters = self._get_s3_parameters()

        try:
            s3 = S3(
                access_key=s3_parameters["access-key"],
                secret_key=s3_parameters["secret-key"],
                endpoint=s3_parameters["endpoint"],
                region=s3_parameters.get("region"),
                skip_verify=skip_verify,
                application=self._charm.app.name,
            )
        except S3Error:
            raise ManagerError("Failed to create S3 session")

        try:
            snapshot = s3.get_content(
                bucket_name=s3_parameters["bucket"],
                object_key=backup_key,
            )
        except S3Error as e:
            raise ManagerError(f"Failed to retrieve snapshot from S3: {e}")
        if not snapshot:
            raise ManagerError(
                f"Snapshot not found in S3 bucket. ({s3_parameters['bucket']}/{backup_key})"
            )

        try:
            openbao_client.restore_snapshot(snapshot=snapshot)
        except OpenBaoClientError as e:
            raise ManagerError(f"Failed to restore snapshot: {e}")

    def _validate_s3_prerequisites(self) -> str | None:
        """Validate the S3 pre-requisites are met.

        Raises:
            ManagerError: If any of the pre-requisites are not met.
        """
        if not self._juju_facade.is_leader:
            raise ManagerError("Only leader unit can perform backup operations")
        if not self._juju_facade.relation_exists(self._relation_name):
            raise ManagerError("S3 relation not created")
        if missing_parameters := self._get_missing_s3_parameters():
            raise ManagerError("S3 parameters missing ({})".format(", ".join(missing_parameters)))

    def _get_missing_s3_parameters(self) -> list[str]:
        """Return the list of missing S3 parameters.

        Returns:
            List[str]: List of missing required S3 parameters.
        """
        s3_parameters = self._s3_requirer.get_s3_connection_info()
        return [param for param in self.REQUIRED_S3_PARAMETERS if param not in s3_parameters]

    def _get_s3_parameters(self) -> dict[str, str]:
        """Retrieve S3 parameters from the S3 integrator relation.

        Removes leading and trailing whitespaces from the parameters.

        Returns:
            Dict[str, str]: Dictionary of the S3 parameters.
        """
        s3_parameters = self._s3_requirer.get_s3_connection_info()
        for key, value in s3_parameters.items():
            if isinstance(value, str):
                s3_parameters[key] = value.strip()
        return s3_parameters
