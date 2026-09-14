# Unsealing

By default, OpenBao units are sealed upon OpenBao initialisation and every time units restart. Unseal each unit with the charm `unseal` action. Initialise a new deployment with the charm `initialize` action; it stores the root token and key shares in a Juju secret that expires. Reveal and store those credentials offline before expiry.

For more information about this topic, read the following documents:
- [The OpenBao seal/unseal concept](https://openbao.org/docs/concepts/seal)
- [How-to: Unseal a sealed unit (k8s)](../how-to/unseal_k8s.md)
- [How-to: Unseal a sealed unit (machine)](../how-to/unseal_machine.md)

To avoid manual unseal after restarts, the machine charm can auto-unseal with:

- [Transit auto-unseal](../how-to/configure_for_autounseal.md) (another OpenBao)
- [PKCS#11 HSM auto-unseal](../how-to/configure_pkcs11_hsm.md) (machine charm only)

PKCS#11 auto-unseal is one-way: after it is enabled and OpenBao is initialized, you cannot go back to Shamir by removing the configuration.
