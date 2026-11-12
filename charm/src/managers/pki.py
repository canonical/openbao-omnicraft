# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Sign certificates for the openbao-pki requirers with the PKI secrets engine."""

import logging
from functools import cached_property

from charmlibs.interfaces.tls_certificates import (
    Certificate,
    CertificateError,
    CertificateRequestAttributes,
    CertificateRequestErrorCode,
    PrivateKey,
    ProviderCertificate,
    ProviderCertificateError,
    RequirerCertificateRequest,
    TLSCertificatesError,
    TLSCertificatesProvidesV4,
    TLSCertificatesRequiresV4,
)
from ops import CharmBase

from common.juju_facade import (
    FacadeError,
    JujuFacade,
    NoSuchSecretError,
    SecretRemovedError,
)
from common.openbao_client import (
    OpenBaoClient,
    OpenBaoClientError,
    PKICertificateError,
    SecretsBackend,
)
from literals import TLS_CERTIFICATES_PKI_RELATION_NAME

logger = logging.getLogger(__name__)


_OPENBAO_PROVIDER_NAME = "openbao-pki"
_CERTIFICATE_ERROR_MESSAGES = {
    CertificateRequestErrorCode.IP_NOT_ALLOWED: (
        "OpenBao PKI role rejected the IP Subject Alternative Name."
    ),
    CertificateRequestErrorCode.DOMAIN_NOT_ALLOWED: (
        "OpenBao PKI role rejected the requested DNS names."
    ),
    CertificateRequestErrorCode.OTHER: ("OpenBao PKI role rejected the certificate request."),
}


def _map_openbao_pki_errors(
    error: PKICertificateError,
) -> CertificateError:
    """Map OpenBao PKI errors to canonical certificate errors from the tls library.

    Args:
        error: PKICertificateError, The error object wrapping different OpenBao PKI errors.

    Returns:
        CertificateError with the appropriate error code. Defaults to OTHER if
        the error cannot be classified into a specific category.
    """
    error_text = str(error).lower()

    # Use mutually exclusive conditions to avoid order dependency
    is_ip_error = (
        "ip_san" in error_text
        or "ip san" in error_text
        or "ip subject alternative name" in error_text
    )
    is_domain_error = (
        "allowed_domain" in error_text
        or "dns name" in error_text
        or "common name" in error_text
        or ("subject alternative name" in error_text and not is_ip_error)
    )

    if is_ip_error:
        code = CertificateRequestErrorCode.IP_NOT_ALLOWED
    elif is_domain_error:
        code = CertificateRequestErrorCode.DOMAIN_NOT_ALLOWED
    else:
        code = CertificateRequestErrorCode.OTHER

    return CertificateError(
        code=code,
        name=code.name,
        message=_CERTIFICATE_ERROR_MESSAGES[code],
        reason=str(error),
        provider=_OPENBAO_PROVIDER_NAME,
    )


class _PKIUtils:
    """Encapsulates some common util functions among PKIManager and ACMEManager."""

    def __init__(self, openbao_client: OpenBaoClient, mount_point: str):
        self._openbao_client = openbao_client
        self._mount_point = mount_point

    def get_openbao_service_ca_certificate(self) -> Certificate | None:
        """Get the current intermediate CA certificate from the OpenBao service."""
        try:
            intermediate_ca_cert = self._openbao_client.get_intermediate_ca(
                mount=self._mount_point
            )
            if not intermediate_ca_cert:
                return None
            return Certificate.from_string(intermediate_ca_cert)
        except (OpenBaoClientError, TLSCertificatesError) as e:
            logger.error("Failed to get current CA certificate: %s", e)
        return None

    def make_latest_issuer_default(self):
        """Make the latest PKI issuer the default issuer.

        This ensures that the latest issuer we have created is used for signing
        certificates.
        """
        try:
            first_issuer = self._openbao_client.list_pki_issuers(mount=self._mount_point)[0]
        except (OpenBaoClientError, IndexError) as e:
            logger.error("Failed to get the first issuer: %s", e)
            return
        try:
            issuers_config = self._openbao_client.read(path=f"{self._mount_point}/config/issuers")
            if issuers_config and not issuers_config["default_follows_latest_issuer"]:
                logger.debug("Updating issuers config")
                self._openbao_client.write(
                    path=f"{self._mount_point}/config/issuers",
                    data={
                        "default_follows_latest_issuer": True,
                        "default": first_issuer,
                    },
                )
        except (TypeError, KeyError):
            logger.error("Issuers config is not yet created")

    def calculate_certificates_ttl(self, certificate: Certificate) -> int:
        """Calculate the maximum allowed validity of certificates issued by OpenBao.

        The current implementation returns half the validity of the CA certificate.
        """
        if not certificate.expiry_time or not certificate.validity_start_time:
            raise ValueError("Invalid CA certificate with no expiry time or validity start time")
        ca_validity_time = certificate.expiry_time - certificate.validity_start_time
        ca_validity_seconds = ca_validity_time.total_seconds()
        return int(ca_validity_seconds / 2)

    def intermediate_ca_exceeds_role_ttl(
        self, intermediate_ca_certificate: Certificate, current_ttl: int
    ) -> bool:
        """Check if the intermediate CA's remaining validity exceeds the role's max TTL.

        OpenBao PKI enforces that issued certificates cannot outlast their signing CA.
        This method ensures that the intermediate CA's remaining validity period
        is longer than the maximum TTL allowed for certificates issued by this role.
        """
        if (
            not current_ttl
            or not intermediate_ca_certificate.expiry_time
            or not intermediate_ca_certificate.validity_start_time
        ):
            return False
        certificate_validity = (
            intermediate_ca_certificate.expiry_time
            - intermediate_ca_certificate.validity_start_time
        )
        certificate_validity_seconds = certificate_validity.total_seconds()
        return certificate_validity_seconds > current_ttl


class PKIManager:
    """Encapsulates the business logic for managing PKI certificates in OpenBao from a Charm."""

    SELF_SIGNED_CA_SECRET_LABEL = "openbao-pki-self-signed-ca"

    def __init__(
        self,
        charm: CharmBase,
        openbao_client: OpenBaoClient,
        certificate_request_attributes: CertificateRequestAttributes,
        mount_point: str,
        role_name: str,
        allowed_domains: str | None,
        allow_bare_domains: bool | None,
        allow_subdomains: bool | None,
        allow_wildcard_certificates: bool | None,
        allow_any_name: bool | None,
        allow_ip_sans: bool | None,
        organization: str | None,
        organizational_unit: str | None,
        country: str | None,
        province: str | None,
        locality: str | None,
        openbao_pki: TLSCertificatesProvidesV4,
        tls_certificates_pki: TLSCertificatesRequiresV4 | None = None,
        self_signed_ca_validity_hours: int = 87600,
    ):
        """Create a new PKIManager object.

        Args:
            charm: The charm this manager is associated with
            openbao_client: The OpenBao client object
            certificate_request_attributes: The certificate request attributes
                that were used when requesting an intermediate certificate from
                the tls_certificates_pki relation provider.
            mount_point: The mount point in OpenBao for the PKI backend
            role_name: The role name for the PKI backend
            openbao_pki: The openbao_pki provider relation helper library
            tls_certificates_pki: The tls_certificates_pki requirer relation helper library.
                If provided, external CA mode is used. If None, self-signed CA mode is used.
            allowed_domains: The domains that the PKI engine will allow certificates to be issued for
            allow_bare_domains: Whether the PKI engine will allow the exact domains in allowed_domains
            allow_subdomains: Whether the PKI engine will allow subdomains to be issued for
            allow_wildcard_certificates: Whether the PKI engine will allow wildcard certificates to be issued
            allow_any_name: Whether the PKI engine will allow any name to be issued for
            allow_ip_sans: Whether the PKI engine will allow IP Subject Alternative Names
            organization: The organization to be included in the issued certificate
            organizational_unit: The organizational unit to be included in the issued certificate
            country: The country to be included in the issued certificate
            province: The province to be included in the issued certificate
            locality: The locality to be included in the issued certificate
            self_signed_ca_validity_hours: The validity period in hours for self-signed CA.
                Only used when tls_certificates_pki is None. Defaults to 87600 (10 years).
        """
        self._openbao_client = openbao_client
        self._juju_facade = JujuFacade(charm)
        self._mount_point = mount_point
        self._role_name = role_name
        self._openbao_pki = openbao_pki
        self._tls_certificates_pki = tls_certificates_pki
        self._certificate_request_attributes = certificate_request_attributes
        self._pki_utils = _PKIUtils(openbao_client, mount_point)
        self._allowed_domains = (
            allowed_domains if allowed_domains else certificate_request_attributes.common_name
        )
        self._allow_bare_domains = allow_bare_domains if allow_bare_domains is not None else True
        self._allow_subdomains = allow_subdomains if allow_subdomains is not None else False
        self._allow_wildcard_certificates = (
            allow_wildcard_certificates if allow_wildcard_certificates is not None else True
        )
        self._allow_any_name = allow_any_name if allow_any_name is not None else False
        self._allow_ip_sans = allow_ip_sans if allow_ip_sans is not None else False
        self._organization = organization if organization is not None else None
        self._organizational_unit = (
            organizational_unit if organizational_unit is not None else None
        )
        self._country = country if country is not None else None
        self._province = province if province is not None else None
        self._locality = locality if locality is not None else None
        self._self_signed_ca_validity_hours = self_signed_ca_validity_hours

    @property
    def _self_signed_ca(self) -> bool:
        """Return True if self-signed CA mode should be used (no external CA relation)."""
        return self._tls_certificates_pki is None

    def _get_pki_intermediate_ca_from_relation(
        self,
    ) -> tuple[ProviderCertificate | None, PrivateKey | None]:
        """Get the intermediate CA certificate and private key from the relation data.

        This is the CA certificate that the provider charm has issued to OpenBao.
        """
        if not self._tls_certificates_pki:
            return None, None
        provider_certificate, private_key = self._tls_certificates_pki.get_assigned_certificate(
            certificate_request=self._certificate_request_attributes
        )
        if not provider_certificate:
            logger.debug("No intermediate CA certificate available for OpenBao PKI")
        if not private_key:
            logger.debug("No private key available for OpenBao PKI")
        return provider_certificate, private_key

    def configure(self):
        """Enable the PKI backend and update the PKI role in OpenBao.

        This method retrieves the intermediate certificate from the relation
        and configures the PKI role in OpenBao if they have changed (or haven't
        yet been configured). It will also revoke all existing certificates if
        the provider has issued a new CA certificate, and ensure all future
        certificates are issued by the new CA certificate.

        Additionally, this method ensures that the intermediate CA certificate
        is renewed if necessary.
        """
        if not self._juju_facade.is_leader:
            logger.debug("Only leader unit can handle a openbao-pki certificate requests")
            return
        self._openbao_client.enable_secrets_engine(SecretsBackend.PKI, self._mount_point)
        logger.info("Enabled PKI secrets engine at %s", self._mount_point)

        if self._self_signed_ca:
            self._configure_self_signed_ca()
        else:
            self._configure_external_ca()

    def _configure_external_ca(self):
        """Configure the PKI backend using an external CA from the relation."""
        if not self._tls_certificates_pki:
            logger.debug("No PKI relation exists: `%s`", TLS_CERTIFICATES_PKI_RELATION_NAME)
            return
        update_ca_certificate = True
        certificate_from_provider, private_key = self._get_pki_intermediate_ca_from_relation()
        if not certificate_from_provider or not private_key:
            return
        openbao_service_ca_certificate = self._pki_utils.get_openbao_service_ca_certificate()
        if (
            openbao_service_ca_certificate
            and openbao_service_ca_certificate == certificate_from_provider.certificate
        ):
            current_ttl = self._openbao_client.get_role_max_ttl(
                role=self._role_name, mount=self._mount_point
            )
            if current_ttl and not self._pki_utils.intermediate_ca_exceeds_role_ttl(
                openbao_service_ca_certificate, current_ttl
            ):
                self._tls_certificates_pki.renew_certificate(
                    certificate_from_provider,
                )
                logger.debug("Renewing CA certificate")
                update_ca_certificate = False
            logger.debug("CA certificate already set in the PKI secrets engine")
            update_ca_certificate = False

        if update_ca_certificate:
            self._openbao_client.import_ca_certificate_and_key(
                certificate=str(certificate_from_provider.certificate),
                private_key=str(private_key),
                mount=self._mount_point,
            )
        issued_certificates_validity = self._pki_utils.calculate_certificates_ttl(
            certificate_from_provider.certificate
        )
        self._create_or_update_pki_role(issued_certificates_validity)
        self.make_latest_pki_issuer_default()

    def _configure_self_signed_ca(self):
        """Configure the PKI backend using a OpenBao self-signed CA."""
        ca_certificate = self._ensure_self_signed_ca_in_openbao()
        issued_certificates_validity = self._pki_utils.calculate_certificates_ttl(ca_certificate)
        self._create_or_update_pki_role(issued_certificates_validity)
        self.make_latest_pki_issuer_default()

    def _ensure_self_signed_ca_in_openbao(self) -> Certificate:
        """Ensure a self-signed CA exists in both Juju secrets and OpenBao.

        Handles three scenarios:
        1. CA exists in secret and OpenBao - no action needed (idempotent)
        2. CA exists in secret but not OpenBao - re-import (e.g. after Raft restore)
        3. No CA or config changed - generate a new one

        Returns:
            The CA certificate that is now active in OpenBao.
        """
        existing_cert, existing_key = self._get_self_signed_ca_from_secret()

        if existing_cert and existing_key:
            # Config changed (e.g. common_name rotation) - regenerate
            if existing_cert.common_name != self._certificate_request_attributes.common_name:
                logger.info(
                    "Self-signed CA common name changed from %s to %s, regenerating",
                    existing_cert.common_name,
                    self._certificate_request_attributes.common_name,
                )
                return self._generate_and_store_self_signed_ca()

            # CA in secret matches OpenBao - nothing to do
            openbao_service_ca_certificate = self._pki_utils.get_openbao_service_ca_certificate()
            if openbao_service_ca_certificate and openbao_service_ca_certificate == existing_cert:
                logger.debug("Self-signed CA already configured in the PKI secrets engine")
                return existing_cert

            # CA exists in secret but not in OpenBao (e.g. after Raft snapshot restore)
            self._openbao_client.import_ca_certificate_and_key(
                certificate=str(existing_cert),
                private_key=str(existing_key),
                mount=self._mount_point,
            )
            return existing_cert

        # No CA exists yet - generate a new one
        return self._generate_and_store_self_signed_ca()

    def _generate_and_store_self_signed_ca(self) -> Certificate:
        """Generate a new self-signed CA, store in Juju secret, and return the certificate."""
        new_cert, new_key = self._generate_self_signed_ca()
        # generate_root already creates the issuer in OpenBao, no need to import
        self._juju_facade.set_app_secret_content(
            {"certificate": new_cert, "privatekey": new_key},
            label=self.SELF_SIGNED_CA_SECRET_LABEL,
        )
        return Certificate.from_string(new_cert)

    def _generate_self_signed_ca(self) -> tuple[str, str]:
        """Generate a new self-signed CA using OpenBao.

        Returns:
            tuple: (certificate_pem, private_key_pem)
        """
        attrs = self._certificate_request_attributes
        return self._openbao_client.generate_self_signed_ca(
            mount=self._mount_point,
            common_name=attrs.common_name,
            ttl=f"{self._self_signed_ca_validity_hours}h",
            sans_dns=list(attrs.sans_dns) if attrs.sans_dns else None,
            country=attrs.country_name,
            province=attrs.state_or_province_name,
            locality=attrs.locality_name,
            organization=attrs.organization,
            organizational_unit=attrs.organizational_unit,
        )

    def _get_self_signed_ca_from_secret(self) -> tuple[Certificate | None, str | None]:
        """Retrieve the self-signed CA from Juju secrets.

        Returns:
            tuple: (certificate, private_key) or (None, None) if not found.
        """
        try:
            cert_pem, key_pem = self._juju_facade.get_secret_content_values(
                "certificate",
                "privatekey",
                label=self.SELF_SIGNED_CA_SECRET_LABEL,
            )
            if cert_pem and key_pem:
                return Certificate.from_string(cert_pem), key_pem
        except (FacadeError, SecretRemovedError, NoSuchSecretError, TLSCertificatesError):
            logger.debug("Self-signed CA secret not found or incomplete")
        return None, None

    def _create_or_update_pki_role(self, issued_certificates_validity: int) -> None:
        """Create or update the PKI role if the configuration has changed."""
        if not self._openbao_client.role_config_matches_given_config(
            role=self._role_name,
            mount=self._mount_point,
            allowed_domains=self._allowed_domains_list,
            allow_bare_domains=self._allow_bare_domains,
            allow_subdomains=self._allow_subdomains,
            allow_wildcard_certificates=self._allow_wildcard_certificates,
            allow_any_name=self._allow_any_name,
            allow_ip_sans=self._allow_ip_sans,
            organization=self._organization,
            organizational_unit=self._organizational_unit,
            country=self._country,
            province=self._province,
            locality=self._locality,
        ) or issued_certificates_validity != self._openbao_client.get_role_max_ttl(
            role=self._role_name, mount=self._mount_point
        ):
            self._openbao_client.create_or_update_pki_charm_role(
                allowed_domains=self._allowed_domains,
                mount=self._mount_point,
                role=self._role_name,
                max_ttl=f"{issued_certificates_validity}s",
                allow_bare_domains=self._allow_bare_domains,
                allow_subdomains=self._allow_subdomains,
                allow_wildcard_certificates=self._allow_wildcard_certificates,
                allow_any_name=self._allow_any_name,
                allow_ip_sans=self._allow_ip_sans,
                organization=self._organization,
                organizational_unit=self._organizational_unit,
                country=self._country,
                province=self._province,
                locality=self._locality,
            )

    def make_latest_pki_issuer_default(self):
        """Make the latest issuer the default issuer."""
        self._pki_utils.make_latest_issuer_default()

    def sync(self):
        """Sync the state of the PKI backend with the TLS certificates relations.

        Issues certificates for all outstanding requests.
        """
        if not self._juju_facade.is_leader:
            logger.debug("Only leader unit can handle a openbao-pki request")
            return
        if not self._self_signed_ca and not self._juju_facade.relation_exists(
            TLS_CERTIFICATES_PKI_RELATION_NAME
        ):
            logger.debug("TLS Certificates PKI relation not created")
            return
        outstanding_pki_requests = self._openbao_pki.get_outstanding_certificate_requests()
        for pki_request in outstanding_pki_requests:
            self._generate_pki_certificate_for_requirer(
                requirer_csr=pki_request,
            )

    def _get_ca_certificate(self) -> Certificate | None:
        """Get the CA certificate for signing, from relation or self-signed secret."""
        if self._self_signed_ca:
            cert, _ = self._get_self_signed_ca_from_secret()
            if cert:
                return cert
            openbao_ca = self._pki_utils.get_openbao_service_ca_certificate()
            if openbao_ca:
                return openbao_ca
            return None
        provider_certificate, _ = self._get_pki_intermediate_ca_from_relation()
        return provider_certificate.certificate if provider_certificate else None

    def _generate_pki_certificate_for_requirer(self, requirer_csr: RequirerCertificateRequest):
        if not self._openbao_client.is_pki_role_created(
            role=self._role_name, mount=self._mount_point
        ):
            logger.debug("PKI role not created")
            return
        ca_certificate = self._get_ca_certificate()
        if not ca_certificate:
            logger.debug(
                "No CA certificate available for the PKI backend, cannot sign certificate requests"
            )
            return
        allowed_cert_validity = self._pki_utils.calculate_certificates_ttl(ca_certificate)
        try:
            certificate = self._openbao_client.sign_pki_certificate_signing_request(
                mount=self._mount_point,
                role=self._role_name,
                csr=str(requirer_csr.certificate_signing_request),
                common_name=requirer_csr.certificate_signing_request.common_name,
                ttl=f"{allowed_cert_validity}s",
            )
        except PKICertificateError as error:
            logger.warning(
                "Failed to sign certificate request %s",
                requirer_csr.certificate_signing_request.raw,
            )
            self._report_certificate_request_error(requirer_csr, error)
            return
        provider_certificate = ProviderCertificate(
            relation_id=requirer_csr.relation_id,
            certificate=Certificate.from_string(certificate.certificate),
            certificate_signing_request=requirer_csr.certificate_signing_request,
            ca=Certificate.from_string(certificate.ca),
            chain=[Certificate.from_string(certificate.certificate)]
            + [Certificate.from_string(cert) for cert in certificate.chain],
        )
        self._openbao_pki.set_relation_certificate(
            provider_certificate=provider_certificate,
        )

    def _report_certificate_request_error(
        self, requirer_csr: RequirerCertificateRequest, error: PKICertificateError
    ) -> None:
        """Set certificate error for a given certificate request."""
        certificate_error = _map_openbao_pki_errors(error)
        self._openbao_pki.set_relation_error(
            provider_error=ProviderCertificateError(
                relation_id=requirer_csr.relation_id,
                certificate_signing_request=requirer_csr.certificate_signing_request,
                error=certificate_error,
            )
        )

    @cached_property
    def _allowed_domains_list(self) -> list[str]:
        """Return the allowed domains for the ACME server in OpenBao as a list."""
        return [domain.strip() for domain in self._allowed_domains.split(",")]
