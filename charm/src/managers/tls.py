# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manage the TLS certificates OpenBao serves its API with."""

import logging
import os
from datetime import timedelta
from enum import Enum, auto
from typing import FrozenSet

from charmlibs.interfaces.certificate_transfer import CertificateTransferProvides
from charmlibs.interfaces.tls_certificates import (
    Certificate,
    CertificateRequestAttributes,
    PrivateKey,
    TLSCertificatesRequiresV4,
    generate_ca,
    generate_certificate,
    generate_csr,
    generate_private_key,
)
from ops import CharmBase, EventBase, Object
from ops.pebble import PathError

from common.exceptions import OpenBaoCertsError
from common.juju_facade import (
    JujuFacade,
    NoSuchSecretError,
    NoSuchStorageError,
    TransientJujuError,
)
from core.base_workload import WorkloadBase
from literals import (
    CA_CERTIFICATE_JUJU_SECRET_LABEL,
    SEND_CA_CERT_RELATION_NAME,
    TLS_CERTIFICATE_ACCESS_RELATION_NAME,
)

logger = logging.getLogger(__name__)


OPENBAO_CA_SUBJECT = "OpenBao self signed CA"


class TLSMode(Enum):
    """This class defines the different modes of TLS configuration.

    SELF_SIGNED: The charm will generate a self signed certificate.
    TLS_INTEGRATION: The charm will use the TLS integration relation.
    """

    SELF_SIGNED = 1
    TLS_INTEGRATION = 2


class File(Enum):
    """This enum determines which files are expected of the library to read."""

    CERT = auto()
    KEY = auto()
    CA = auto()
    AUTOUNSEAL_CA = auto()


class TLSManager(Object):
    """This class configures the certificates within OpenBao."""

    def __init__(
        self,
        charm: CharmBase,
        workload: WorkloadBase,
        common_name: str,
        sans_dns: FrozenSet[str] = frozenset(),
        sans_ip: FrozenSet[str] = frozenset(),
        country_name: str = "",
        state_or_province_name: str = "",
        locality_name: str = "",
        organization: str = "",
        organizational_unit: str = "",
        email_address: str = "",
    ):
        """Create a new TLSManager object.

        Args:
            charm: CharmBase
            workload: The OpenBao workload, which stores the certificates
                in its `tls_directory_path`.
            common_name: The common name of the certificate
            sans_dns: Subject alternative names of the certificate
            sans_ip: Subject alternative IP addresses of the certificate
            country_name: Country name of the certificate
            state_or_province_name: State or province name of the certificate
            locality_name: Locality name of the certificate
            organization: Organization name of the certificate
            organizational_unit: Organizational unit name of the certificate
            email_address: Email address of the certificate
        """
        super().__init__(charm, "tls")
        self.charm = charm
        self.juju_facade = JujuFacade(charm)
        self.workload = workload
        self.tls_directory_path = workload.tls_directory_path
        self.common_name = common_name
        self.sans_dns = sans_dns
        self.sans_ip = sans_ip
        self.country_name = country_name
        self.state_or_province_name = state_or_province_name
        self.locality_name = locality_name
        self.organization = organization
        self.organizational_unit = organizational_unit
        self.email_address = email_address
        self.mode = self._get_mode()
        self.certificate_transfer = CertificateTransferProvides(charm, SEND_CA_CERT_RELATION_NAME)
        if self.mode == TLSMode.TLS_INTEGRATION:
            self.tls_access = TLSCertificatesRequiresV4(
                charm=charm,
                relationship_name=TLS_CERTIFICATE_ACCESS_RELATION_NAME,
                certificate_requests=self._get_certificate_requests(),
            )
            self.framework.observe(
                self.charm.on[TLS_CERTIFICATE_ACCESS_RELATION_NAME].relation_changed,
                self._configure_tls_integration,
            )
        elif self.mode == TLSMode.SELF_SIGNED:
            self.tls_access = None
            self.framework.observe(
                self.charm.on.config_changed, self._configure_self_signed_certificates
            )
            self.framework.observe(
                self.charm.on.update_status, self._configure_self_signed_certificates
            )
            self.framework.observe(
                self.charm.on[TLS_CERTIFICATE_ACCESS_RELATION_NAME].relation_broken,
                self._configure_self_signed_certificates,
            )
        self.framework.observe(
            self.charm.on[SEND_CA_CERT_RELATION_NAME].relation_joined,
            self._configure_ca_cert_relation,
        )
        self.framework.observe(
            self.charm.on.update_status,
            self._configure_ca_cert_relation,
        )

    def _configure_ca_cert_relation(self, event: EventBase):
        """Send the CA certificate to the relation."""
        self.send_ca_cert()

    def _get_certificate_requests(self) -> list[CertificateRequestAttributes]:
        if not self.common_name:
            return []
        if not self.sans_dns:
            return []
        return [
            CertificateRequestAttributes(
                common_name=self.common_name,
                sans_dns=self.sans_dns,
                sans_ip=self.sans_ip,
                country_name=self.country_name if self.country_name else None,
                state_or_province_name=self.state_or_province_name
                if self.state_or_province_name
                else None,
                locality_name=self.locality_name if self.locality_name else None,
                organization=self.organization if self.organization else None,
                organizational_unit=self.organizational_unit if self.organizational_unit else None,
                email_address=self.email_address if self.email_address else None,
            )
        ]

    def _get_mode(self) -> TLSMode:
        """Determine the TLS mode of the charm."""
        if self.juju_facade.relation_exists(TLS_CERTIFICATE_ACCESS_RELATION_NAME):
            return TLSMode.TLS_INTEGRATION
        return TLSMode.SELF_SIGNED

    def _configure_self_signed_certificates(self, _: EventBase) -> None:  # noqa: C901
        """Configure the charm with self signed certificates."""
        if not self.workload.is_accessible():
            logger.debug("Workload is not accessible")
            return
        if self.tls_access:
            logger.warning(
                "TLS access relation found, skipping self signed certificate generation."
            )
            return
        workload_ca_certificate = self.pull_tls_file_from_workload(File.CA)
        try:
            secret_ca_certificate = ca_certificate = self.juju_facade.get_secret_content_values(
                "certificate",
                label=CA_CERTIFICATE_JUJU_SECRET_LABEL,
            )[0]
        except NoSuchSecretError:
            secret_ca_certificate = ca_certificate = None

        if self.charm.unit.is_leader():
            if not secret_ca_certificate:
                ca_private_key, ca_certificate = generate_openbao_ca_certificate()
                logger.info("Generated new CA certificate.")
                self.juju_facade.set_app_secret_content(
                    {"privatekey": ca_private_key, "certificate": ca_certificate},
                    CA_CERTIFICATE_JUJU_SECRET_LABEL,
                )
        if (
            workload_ca_certificate
            and secret_ca_certificate
            and existing_certificate_is_self_signed(
                ca_certificate=Certificate.from_string(workload_ca_certificate)
            )
            and Certificate.from_string(workload_ca_certificate)
            == Certificate.from_string(secret_ca_certificate)
        ):
            workload_unit_cert = self.pull_tls_file_from_workload(File.CERT)
            if workload_unit_cert and self._unit_certificate_sans_match_current_request(
                workload_unit_cert
            ):
                logger.debug(
                    "Found existing self signed certificate in workload with matching attributes."
                )
                return
        if not self.ca_certificate_secret_exists():
            logger.debug("No CA certificate found.")
            return
        try:
            ca_private_key, ca_certificate = self.juju_facade.get_secret_content_values(
                "privatekey",
                "certificate",
                label=CA_CERTIFICATE_JUJU_SECRET_LABEL,
            )
        except NoSuchSecretError:
            logger.error("Charm does not have permission to access the CA certificate secret.")
            return
        if not ca_certificate:
            logger.debug("No CA certificate found.")
            return
        if not ca_private_key:
            logger.debug("No CA private key found.")
            return
        unit_private_key, unit_certificate = generate_openbao_unit_certificate(
            common_name=self.common_name,
            sans_dns=self.sans_dns,
            sans_ip=self.sans_ip,
            ca_certificate=ca_certificate,
            ca_private_key=ca_private_key,
        )
        self._push_tls_file_to_workload(File.KEY, unit_private_key)
        self._push_tls_file_to_workload(File.CERT, unit_certificate)
        self._push_tls_file_to_workload(File.CA, ca_certificate)
        logger.info(
            "Saved OpenBao generated CA and self signed certificate to %s.",
            self.juju_facade.unit_name,
        )
        self._restart_openbao()

    def _configure_tls_integration(self, _: EventBase) -> None:
        """Configure the charm with the TLS integration relation.

        Retrieve assigned certificate and private key from the relation and save them to the workload.
        """
        if not self.workload.is_accessible():
            logger.debug("Workload is not accessible")
            return
        if not self.tls_access:
            logger.debug("No TLS access relation.")
            return
        certificate_requests = self._get_certificate_requests()
        if not certificate_requests:
            logger.debug("No certificate requests.")
            return
        assigned_certificate, private_key = self.tls_access.get_assigned_certificate(
            certificate_request=certificate_requests[0]
        )
        if not assigned_certificate:
            logger.debug("No certificate assigned.")
            return
        if not private_key:
            logger.debug("No private key assigned.")
            return
        restart = False
        if str(private_key) != self.pull_tls_file_from_workload(File.KEY):
            self._push_tls_file_to_workload(File.KEY, str(private_key))
            logger.info(
                "Private key from access relation saved for unit %s.",
                self.charm.unit.name,
            )
            restart = True
        if str(assigned_certificate.certificate) != self.pull_tls_file_from_workload(File.CERT):
            self._push_tls_file_to_workload(File.CERT, str(assigned_certificate.certificate))
            logger.info(
                "Certificate from access relation saved for unit %s.",
                self.charm.unit.name,
            )
            restart = True
        if self.pull_tls_file_from_workload(File.CA) != str(assigned_certificate.ca):
            self._push_tls_file_to_workload(File.CA, str(assigned_certificate.ca))
            restart = True
        if restart:
            self._restart_openbao()

    def send_ca_cert(self):
        """Send the existing CA cert in the workload to all relations."""
        if ca := self.pull_tls_file_from_workload(File.CA):
            for relation in self.juju_facade.get_relations(SEND_CA_CERT_RELATION_NAME):
                self.certificate_transfer.add_certificates(
                    certificates={ca}, relation_id=relation.id
                )
                logger.info("Sent CA certificate to relation %s", relation.id)
        else:
            for relation in self.juju_facade.get_relations(SEND_CA_CERT_RELATION_NAME):
                self.certificate_transfer.remove_all_certificates(relation_id=relation.id)
                logger.info("Removed CA cert from relation %s", relation.id)

    def get_tls_file_path_in_workload(self, file: File) -> str:
        """Return the requested file's location in the workload.

        Args:
            file: a File object that determines which file path to return
        Returns:
            the path of the file from the workload's perspective
        """
        return f"{self.tls_directory_path}/{file.name.lower()}.pem"

    def get_tls_file_path_in_charm(self, file: File) -> str:
        """Return the requested file's location in the charm (not in the workload).

        This path would typically be: /var/lib/juju/storage/certs/0/{file}.pem

        Args:
            file: a File object that determines which file path to return
        Returns:
            str: path
        Raises:
            OpenBaoCertsError: If the CA certificate is not found
            TransientJujuError: If the storage is not attached yet
        """
        try:
            storage_location = self.juju_facade.get_storage_location("certs")
        except NoSuchStorageError:
            raise OpenBaoCertsError()
        except TransientJujuError:
            # This can happen if the storage is not attached yet
            raise
        return f"{storage_location}/{file.name.lower()}.pem"

    def tls_file_available_in_charm(self, file: File) -> bool:
        """Return whether the given file is available in the charm.

        Args:
            file: a File object that determines which file to check
        Returns:
            bool: True if file exists
        """
        try:
            file_path = self.get_tls_file_path_in_charm(file)
            return os.path.exists(file_path)
        except (OpenBaoCertsError, TransientJujuError):
            return False

    def ca_certificate_is_saved(self) -> bool:
        """Return whether a CA cert and its private key are saved in the charm."""
        return self.ca_certificate_secret_exists() or self.tls_file_pushed_to_workload(File.CA)

    def _restart_openbao(self) -> None:
        """Attempt to restart the OpenBao server."""
        try:
            self.workload.restart()
            logger.debug("OpenBao restarted")
        except Exception:
            logger.debug("Couldn't restart OpenBao. Proceeding normally.")

    def pull_tls_file_from_workload(self, file: File) -> str:
        """Get a file related to certs from the workload.

        Args:
            file: a File object that determines which file to read.

        Returns:
            str: The file content without whitespace
                Or an empty string if the file does not exist.
        """
        try:
            with self.workload.pull(
                self.get_tls_file_path_in_workload(file),
            ) as file_content:
                return file_content.read().strip()
        except (PathError, FileNotFoundError):
            return ""

    def ca_certificate_secret_exists(self) -> bool:
        """Return whether CA certificate is stored in secret."""
        return self.juju_facade.secret_exists_with_fields(
            fields=("privatekey", "certificate"),
            label=CA_CERTIFICATE_JUJU_SECRET_LABEL,
        )

    def _push_tls_file_to_workload(self, file: File, data: str) -> None:
        """Push one of the given file types to the workload.

        Args:
            file: a File object that determines which file to write.
            data: the data to write into that file.
        """
        self.workload.push(path=self.get_tls_file_path_in_workload(file), source=data)
        logger.debug("Pushed %s file to workload", file.name)

    def push_autounseal_ca_cert(self, ca_cert: str) -> None:
        """Push the CA certificate to the workload.

        Args:
            ca_cert: The CA certificate to push to the workload.
        """
        self.workload.push(self.get_tls_file_path_in_workload(File.AUTOUNSEAL_CA), ca_cert)

    def _remove_tls_file_from_workload(self, file: File) -> None:
        """Remove the certificate files that are used for authentication.

        Args:
            file: a File object that determines which file to remove.
        """
        try:
            self.workload.remove_path(path=self.get_tls_file_path_in_workload(file))
        except ValueError:
            pass
        logger.debug("Removed %s file from workload.", file.name)

    def tls_file_pushed_to_workload(self, file: File) -> bool:
        """Return whether tls file is pushed to the workload.

        Args:
            file: a File object that determines which file to check.

        Returns:
            bool: True if file exists.
        """
        return self.workload.exists(path=f"{self.tls_directory_path}/{file.name.lower()}.pem")

    def _unit_certificate_sans_match_current_request(self, unit_cert_content: str) -> bool:
        """Check if the unit certificate attributes match the current TLS manager configuration.

        Args:
            unit_cert_content: The PEM content of the unit certificate

        Returns:
            bool: True if certificate attributes match current configuration, False otherwise
        """
        try:
            unit_cert = Certificate.from_string(unit_cert_content)

            cert_sans_dns = set(unit_cert.sans_dns) if unit_cert.sans_dns else set()
            cert_sans_ip = set(unit_cert.sans_ip) if unit_cert.sans_ip else set()
            current_sans_dns = set(self.sans_dns) if self.sans_dns else set()
            current_sans_ip = set(self.sans_ip) if self.sans_ip else set()

            return (
                cert_sans_dns == current_sans_dns
                and cert_sans_ip == current_sans_ip
                and unit_cert.common_name == self.common_name
            )
        except Exception as e:
            logger.warning("Failed to parse unit certificate attributes: %s", e)
            return False


def generate_openbao_ca_certificate() -> tuple[str, str]:
    """Generate OpenBao CA certificates valid for 50 years.

    Returns:
        Tuple[str, str]: CA Private key, CA certificate
    """
    ca_private_key = generate_private_key()
    ca_certificate = generate_ca(
        private_key=ca_private_key,
        common_name=OPENBAO_CA_SUBJECT,
        validity=timedelta(days=365 * 50),
    )
    return str(ca_private_key), str(ca_certificate)


def generate_openbao_unit_certificate(
    common_name: str,
    sans_ip: FrozenSet[str],
    sans_dns: FrozenSet[str],
    ca_certificate: str,
    ca_private_key: str,
) -> tuple[str, str]:
    """Generate OpenBao unit certificates valid for 50 years.

    Args:
        common_name: Common name of the certificate
        sans_ip: Subject alternative IP addresses of the certificate
        sans_dns: Subject alternative names of the certificate
        ca_certificate: CA certificate
        ca_private_key: CA private key

    Returns:
        Tuple[str, str]: Private key, Certificate
    """
    openbao_private_key = generate_private_key()
    csr = generate_csr(
        private_key=openbao_private_key,
        common_name=common_name,
        sans_ip=sans_ip,
        sans_dns=sans_dns,
    )
    openbao_certificate = generate_certificate(
        ca=Certificate.from_string(ca_certificate),
        ca_private_key=PrivateKey.from_string(ca_private_key),
        csr=csr,
        validity=timedelta(days=365 * 50),
    )
    return str(openbao_private_key), str(openbao_certificate)


def existing_certificate_is_self_signed(ca_certificate: Certificate) -> bool:
    """Return whether the certificate is a self signed certificate generated by the OpenBao charm."""
    return ca_certificate.common_name == OPENBAO_CA_SUBJECT
