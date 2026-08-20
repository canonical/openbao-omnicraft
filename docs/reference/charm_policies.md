# Charm Policies

When running the [authorize-charm](https://charmhub.io/openbao/actions) Juju action, the charm creates a OpenBao policy to ensure it can only access what it needs for day-to-day operations.

These rules are defined in the [charm_policy.hcl](https://github.com/canonical/openbao-omnicraft) file under the respective charms' source code.

Paths starting with the `charm—` prefix should only be accessed by the charm; it is strongly discouraged for users to create resources under these paths.
