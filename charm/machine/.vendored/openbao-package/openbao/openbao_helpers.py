"""This library contains helper function used when configuring the OpenBao service."""

import ipaddress
import logging
import os
from dataclasses import dataclass
from typing import Dict, List

import hcl
from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)


HSM_CONFIG_SECRET_PIN_KEY = "pin"
HSM_CONFIG_SECRET_SLOT_KEY = "slot"
HSM_CONFIG_SECRET_TOKEN_LABEL_KEY = "token-label"
HSM_CONFIG_SECRET_KEY_LABEL_KEY = "key-label"
HSM_CONFIG_SECRET_KEY_ID_KEY = "key-id"


@dataclass
class AutounsealConfiguration:
    """Details required for configuring auto-unseal on OpenBao."""

    address: str
    mount_path: str
    key_name: str
    ca_cert_path: str


@dataclass(frozen=True)
class Pkcs11SealConfiguration:
    """Details required for configuring PKCS#11 auto-unseal on OpenBao.

    Plugin fields register the external openbao-plugin-kms-pkcs11 binary shipped
    in the OpenBao snap (required for static bao builds and for OpenBao 2.7+).
    """

    lib: str
    pin: str
    slot: str | None = None
    token_label: str | None = None
    key_label: str | None = None
    key_id: str | None = None
    plugin_directory: str | None = None
    plugin_command: str | None = None
    plugin_version: str | None = None
    plugin_sha256sum: str | None = None


def common_name_config_is_valid(common_name: str) -> bool:
    """Return whether the config value for the common name is valid."""
    return common_name != ""


def sans_dns_config_is_valid(sans_dns: str) -> bool:
    """Return whether the config value for the sans dns is valid.

    Checks that the provided string is a comma separated list of strings,
    and that each string is not empty and does not contain any spaces.
    """
    if not sans_dns:
        return True
    dns_names = (name.strip() for name in sans_dns.split(","))
    return all(name and " " not in name for name in dns_names)


def allowed_domains_config_is_valid(allowed_domains: str) -> bool:
    """Return whether the config value for the allowed domains is valid.

    Checks that the provided string is a comma separated list of strings,
    and that each string is not empty and does not contain any spaces.
    """
    if not allowed_domains:
        return True
    dns_names = (name.strip() for name in allowed_domains.split(","))
    valid = all(name and " " not in name for name in dns_names)
    return valid


def sans_ip_config_is_valid(sans_ip: str) -> bool:
    """Return whether the config value for the sans IPs is valid.

    Checks that the provided string is a comma separated list of valid IPv4/IPv6 addresses,
    and that each string is not empty and does not contain any spaces.
    """
    if not sans_ip:
        return True
    candidates = (token.strip() for token in sans_ip.split(","))
    for token in candidates:
        if not token or " " in token:
            return False
        try:
            ipaddress.ip_address(token)
        except ValueError:
            return False
    return True


def _secret_field(content: dict[str, str], key: str) -> str:
    return (content.get(key) or "").strip()


def hsm_config_secret_validation_error(content: dict[str, str]) -> str | None:
    """Return an error if the HSM secret is missing fields required by PKCS#11.

    OpenBao requires a PIN, at least one of slot or token_label, and at least
    one of key_label or key_id. Empty values are treated as unset.
    """
    pin = _secret_field(content, HSM_CONFIG_SECRET_PIN_KEY)
    slot = _secret_field(content, HSM_CONFIG_SECRET_SLOT_KEY)
    token_label = _secret_field(content, HSM_CONFIG_SECRET_TOKEN_LABEL_KEY)
    key_label = _secret_field(content, HSM_CONFIG_SECRET_KEY_LABEL_KEY)
    key_id = _secret_field(content, HSM_CONFIG_SECRET_KEY_ID_KEY)
    missing: list[str] = []
    if not pin:
        missing.append(HSM_CONFIG_SECRET_PIN_KEY)
    if not slot and not token_label:
        missing.append(f"{HSM_CONFIG_SECRET_SLOT_KEY} or {HSM_CONFIG_SECRET_TOKEN_LABEL_KEY}")
    if not key_label and not key_id:
        missing.append(f"{HSM_CONFIG_SECRET_KEY_LABEL_KEY} or {HSM_CONFIG_SECRET_KEY_ID_KEY}")
    if missing:
        return "hsm-config secret is missing required fields: " + ", ".join(missing)
    return None


def pkcs11_seal_config_from_secret(
    content: dict[str, str],
    lib: str,
    *,
    plugin_directory: str | None = None,
    plugin_command: str | None = None,
    plugin_version: str | None = None,
    plugin_sha256sum: str | None = None,
) -> Pkcs11SealConfiguration | None:
    """Build a PKCS#11 seal configuration from a Juju secret and library path."""
    if not lib or hsm_config_secret_validation_error(content):
        return None
    return Pkcs11SealConfiguration(
        lib=lib,
        pin=_secret_field(content, HSM_CONFIG_SECRET_PIN_KEY),
        slot=_secret_field(content, HSM_CONFIG_SECRET_SLOT_KEY) or None,
        token_label=_secret_field(content, HSM_CONFIG_SECRET_TOKEN_LABEL_KEY) or None,
        key_label=_secret_field(content, HSM_CONFIG_SECRET_KEY_LABEL_KEY) or None,
        key_id=_secret_field(content, HSM_CONFIG_SECRET_KEY_ID_KEY) or None,
        plugin_directory=plugin_directory,
        plugin_command=plugin_command,
        plugin_version=plugin_version,
        plugin_sha256sum=plugin_sha256sum,
    )


def _hcl_escape(value: str) -> str:
    """Escape a string for use inside a quoted HCL value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_openbao_config_file(
    config_template_path: str,
    config_template_name: str,
    default_lease_ttl: str,
    max_lease_ttl: str,
    cluster_address: str,
    api_address: str,
    tls_cert_file: str,
    tls_key_file: str,
    tcp_address: str,
    raft_storage_path: str,
    node_id: str,
    retry_joins: List[Dict[str, str]],
    log_level: str,
    autounseal_config: AutounsealConfiguration | None = None,
    pkcs11_config: Pkcs11SealConfiguration | None = None,
) -> str:
    """Render the OpenBao config file."""
    jinja2_environment = Environment(loader=FileSystemLoader(config_template_path))
    template = jinja2_environment.get_template(config_template_name)
    content = template.render(
        default_lease_ttl=default_lease_ttl,
        max_lease_ttl=max_lease_ttl,
        cluster_address=cluster_address,
        api_address=api_address,
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
        tcp_address=tcp_address,
        raft_storage_path=raft_storage_path,
        node_id=node_id,
        retry_joins=retry_joins,
        log_level=log_level,
        autounseal_address=autounseal_config.address if autounseal_config else None,
        autounseal_mount_path=autounseal_config.mount_path if autounseal_config else None,
        autounseal_key_name=autounseal_config.key_name if autounseal_config else None,
        autounseal_ca_cert_path=autounseal_config.ca_cert_path if autounseal_config else None,
        pkcs11_lib=_hcl_escape(pkcs11_config.lib) if pkcs11_config else None,
        pkcs11_pin=_hcl_escape(pkcs11_config.pin) if pkcs11_config else None,
        pkcs11_slot=(
            _hcl_escape(pkcs11_config.slot) if pkcs11_config and pkcs11_config.slot else None
        ),
        pkcs11_token_label=(
            _hcl_escape(pkcs11_config.token_label)
            if pkcs11_config and pkcs11_config.token_label
            else None
        ),
        pkcs11_key_label=(
            _hcl_escape(pkcs11_config.key_label)
            if pkcs11_config and pkcs11_config.key_label
            else None
        ),
        pkcs11_key_id=(
            _hcl_escape(pkcs11_config.key_id) if pkcs11_config and pkcs11_config.key_id else None
        ),
        pkcs11_plugin_directory=(
            _hcl_escape(pkcs11_config.plugin_directory)
            if pkcs11_config and pkcs11_config.plugin_directory
            else None
        ),
        pkcs11_plugin_command=(
            _hcl_escape(pkcs11_config.plugin_command)
            if pkcs11_config and pkcs11_config.plugin_command
            else None
        ),
        pkcs11_plugin_version=(
            _hcl_escape(pkcs11_config.plugin_version)
            if pkcs11_config and pkcs11_config.plugin_version
            else None
        ),
        pkcs11_plugin_sha256sum=(
            _hcl_escape(pkcs11_config.plugin_sha256sum)
            if pkcs11_config and pkcs11_config.plugin_sha256sum
            else None
        ),
    )
    return content


def seal_type_has_changed(content_a: str, content_b: str) -> bool:
    """Check if the seal type has changed between two versions of the OpenBao configuration file."""
    return _seal_types(_load_hcl(content_a)) != _seal_types(_load_hcl(content_b))


def _load_hcl(content: str) -> dict:
    if not content or not content.strip():
        return {}
    loaded = hcl.loads(content)
    return loaded or {}


def _seal_types(config: dict) -> set[str]:
    seal = config.get("seal")
    if not isinstance(seal, dict):
        return set()
    return set(seal.keys())


def config_file_content_matches(existing_content: str, new_content: str) -> bool:
    """Return whether two OpenBao config file contents match.

    We check if the retry_join addresses match, and then we check if the rest of the config
    file matches.

    Returns:
        bool: Whether the openbao config file content matches
    """
    existing_config_hcl = hcl.loads(existing_content)
    new_content_hcl = hcl.loads(new_content)
    if not existing_config_hcl:
        logger.info("Existing config file is empty")
        return existing_config_hcl == new_content_hcl
    if not new_content_hcl:
        logger.info("New config file is empty")
        return existing_config_hcl == new_content_hcl

    new_retry_joins = new_content_hcl["storage"]["raft"].pop("retry_join", [])

    try:
        existing_retry_joins = existing_config_hcl["storage"]["raft"].pop("retry_join", [])
    except KeyError:
        existing_retry_joins = []

    # If there is only one retry join, it is a dict
    if isinstance(new_retry_joins, dict):
        new_retry_joins = [new_retry_joins]
    if isinstance(existing_retry_joins, dict):
        existing_retry_joins = [existing_retry_joins]

    new_retry_join_api_addresses = {address["leader_api_addr"] for address in new_retry_joins}
    existing_retry_join_api_addresses = {
        address["leader_api_addr"] for address in existing_retry_joins
    }

    return (
        new_retry_join_api_addresses == existing_retry_join_api_addresses
        and new_content_hcl == existing_config_hcl
    )


def get_env_var(env_var: str) -> str | None:
    """Get the environment variable value.

    Converts the `env_var` to upper-case before looking it up.

    Args:
        env_var: Name of the environment variable.

    Returns:
        Value of the environment variable. None if not found.
    """
    return os.environ.get(env_var.upper())
