# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Run the ACME server of OpenBao."""

import logging
from functools import cached_property

from charmlibs.interfaces.tls_certificates import (
    CertificateRequestAttributes,
    PrivateKey,
    ProviderCertificate,
    TLSCertificatesRequiresV4,
)
from ops import CharmBase

from common.exceptions import ManagerError
from common.juju_facade import (
    JujuFacade,
)
from common.openbao_client import (
    OpenBaoClient,
    SecretsBackend,
)
from literals import TLS_CERTIFICATES_ACME_RELATION_NAME
from managers.pki import _PKIUtils

logger = logging.getLogger(__name__)


class ACMEManager:
    """Encapsulates the business logic for managing the ACME engine in OpenBao."""

    def __init__(
        self,
        charm: CharmBase,
        openbao_client: OpenBaoClient,
        mount_point: str,
        tls_certificates_acme: TLSCertificatesRequiresV4,
        certificate_request_attributes: CertificateRequestAttributes,
        role_name: str,
        openbao_address: str,
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
    ):
        self._charm = charm
        self._juju_facade = JujuFacade(charm)
        self._openbao_client = openbao_client
        self._mount_point = mount_point
        self._tls_certificates_acme = tls_certificates_acme
        self._certificate_request_attributes = certificate_request_attributes
        self._role_name = role_name
        self._openbao_address = openbao_address
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

    def _get_acme_intermediate_ca_from_relation(
        self,
    ) -> tuple[ProviderCertificate | None, PrivateKey | None]:
        """Get the intermediate CA certificate and private key from the relation data.

        This is the CA certificate that the provider charm has issued to OpenBao.
        """
        provider_certificate, private_key = self._tls_certificates_acme.get_assigned_certificate(
            certificate_request=self._certificate_request_attributes
        )
        if not provider_certificate:
            logger.debug("No intermediate CA certificate available for OpenBao ACME")
        if not private_key:
            logger.debug("No private key available for OpenBao ACME")
        return provider_certificate, private_key

    def _configure_intermediate_ca_certificate(self) -> None:
        """Configure the intermediate CA certificate for the ACME server in OpenBao."""
        certificate_from_provider, private_key = self._get_acme_intermediate_ca_from_relation()
        if not certificate_from_provider or not private_key:
            raise ManagerError("No intermediate CA certificate available for OpenBao ACME")

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
                self._tls_certificates_acme.renew_certificate(
                    certificate_from_provider,
                )
                logger.debug("Renewing ACME intermediate CA certificate")
                return
            logger.debug("ACME intermediate CA certificate already set")
            return
        self._openbao_client.import_ca_certificate_and_key(
            certificate=str(certificate_from_provider.certificate),
            private_key=str(private_key),
            mount=self._mount_point,
        )

    def _configure_acme_role(self) -> None:
        """Configure the ACME role in OpenBao."""
        certificate_from_provider, _ = self._get_acme_intermediate_ca_from_relation()
        if not certificate_from_provider:
            logger.debug("No intermediate CA certificate available for OpenBao ACME")
            return
        max_ttl = self._pki_utils.calculate_certificates_ttl(certificate_from_provider.certificate)
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
        ) or max_ttl != self._openbao_client.get_role_max_ttl(
            role=self._role_name, mount=self._mount_point
        ):
            self._openbao_client.create_or_update_acme_role(
                role=self._role_name,
                max_ttl=f"{max_ttl}s",
                mount=self._mount_point,
                allow_bare_domains=self._allow_bare_domains,
                allow_subdomains=self._allow_subdomains,
                allow_wildcard_certificates=self._allow_wildcard_certificates,
                allow_any_name=self._allow_any_name,
                allowed_domains=self._allowed_domains,
                allow_ip_sans=self._allow_ip_sans,
                organization=self._organization,
                organizational_unit=self._organizational_unit,
                country=self._country,
                province=self._province,
                locality=self._locality,
            )

    def _enable_acme(self) -> None:
        """Configure ACME to be enabled in OpenBao.

        The default directory policy is bound to the charm's ACME role so that
        the role's allowed_domains / allow_any_name restrictions are enforced.
        """
        self._openbao_client.write(
            path=f"{self._mount_point}/config/acme",
            data={
                "enabled": True,
                "default_directory_policy": f"role:{self._role_name}",
            },
        )

    def make_latest_acme_issuer_default(self):
        """Make the latest issuer the default issuer."""
        self._pki_utils.make_latest_issuer_default()

    def configure(self) -> None:
        """Configure the ACME server in OpenBao."""
        if not self._juju_facade.is_leader:
            logger.debug("Only leader unit can configure ACME")
            return
        if not self._juju_facade.relation_exists(TLS_CERTIFICATES_ACME_RELATION_NAME):
            logger.debug(
                "No TLS relation exists to configure ACME server: `%s`",
                TLS_CERTIFICATES_ACME_RELATION_NAME,
            )
            return
        self._openbao_client.enable_secrets_engine(SecretsBackend.PKI, self._mount_point)

        try:
            self._configure_intermediate_ca_certificate()
        except ManagerError as e:
            logger.error("Failed to configure ACME server: %s", e)
            return

        self._configure_acme_role()

        self._openbao_client.allow_acme_headers(
            mount=self._mount_point,
        )
        self._openbao_client.set_urls(
            issuing_certificates=[f"{self._openbao_address}/v1/pki/ca"],
            crl_distribution_points=[f"{self._openbao_address}/v1/pki/crl"],
            mount=self._mount_point,
        )
        self._openbao_client.write(
            path=f"{self._mount_point}/config/cluster",
            data={"path": f"{self._openbao_address}/v1/{self._mount_point}"},
        )
        self._enable_acme()
        self.make_latest_acme_issuer_default()

    @cached_property
    def _allowed_domains_list(self) -> list[str]:
        """Return the allowed domains for the ACME server in OpenBao as a list."""
        return [domain.strip() for domain in self._allowed_domains.split(",")]
