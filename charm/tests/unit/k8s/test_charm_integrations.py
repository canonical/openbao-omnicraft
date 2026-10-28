import ops.testing as testing
from ops.testing import Context

from charm import OpenBaoCharm
from fixtures import OpenBaoCharmFixtures


class TestCharmIntegrations(OpenBaoCharmFixtures):
    integration_endpoints = [
        "openbao-autounseal-requires",
        "ingress",
        "ingress-per-unit",
        "tls-certificates-access",
        "tls-certificates-pki",
        "tls-certificates-acme",
        "logging",
        "s3-parameters",
        "tracing",
        "openbao-kv",
        "openbao-pki",
        "openbao-autounseal-provides",
        "send-ca-cert",
        "grafana-dashboard",
        "metrics-endpoint",
    ]

    def test_given_integrations_when_start_then_integrations_are_complete(self):
        """Ensure no integrations were removed from the charmcraft.yaml."""
        ctx = Context(OpenBaoCharm)
        relations = []
        for integration in self.integration_endpoints:
            relation = testing.Relation(
                endpoint=integration,
            )
            relations.append(relation)
        state_in = testing.State(
            relations=relations, containers=[testing.Container(name="openbao", can_connect=True)]
        )
        with ctx(ctx.on.start(), state_in) as manager:
            manager.run()

    def test_given_integrations_when_start_then_no_extra_integrations(self):
        """Ensure no integrations were added to the charmcraft.yaml without being added to the test."""
        ctx = Context(OpenBaoCharm)
        with ctx(
            ctx.on.start(),
            testing.State(
                containers=[testing.Container(name="openbao", can_connect=True)],
            ),
        ) as manager:
            missing_relations = set(manager.charm.meta.relations.keys()).difference(
                set(self.integration_endpoints)
            ) - {"openbao-peers"}
            if missing_relations:
                raise AssertionError(
                    f"Charmcraft.yaml contains integrations missing from test: {missing_relations}"
                )
            manager.run()
