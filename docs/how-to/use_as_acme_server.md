# Use OpenBao as an ACME Server to obtain TLS certificates

In this how-to guide, we will configure OpenBao to act as an ACME server using [OpenBao's PKI secrets engine](https://openbao.org/docs/secrets/pki). Here [self-signed-certificates](https://charmhub.io/self-signed-certificates) will be the parent CA.

The certificates issued by OpenBao will have a validity period that is half of its intermediate CA's, which is determined by the root provider's configuration, in this case, the self-signed certificates.

```{note}
OpenBao ACME will allow issuing certificates depending on how it is configured, please see `acme_allow_subdomains`, `acme_allowed_domains`, `acme_allow_any_name` and `acme_allow_wildcard_certificates`
```

1. Configure OpenBao's common name, and the ACME server to allow issuing certificates for subdomains and any domain name

   ```shell
   juju config openbao acme_ca_common_name=<your domain name> acme_allow_subdomains=true acme_allow_any_name=true
   ```

2. Deploy the parent CA

   ```shell
   juju deploy self-signed-certificates --channel 1/stable
   ```

3. Integrate OpenBao with its parent CA

   ```shell
   juju integrate openbao:tls-certificates-acme self-signed-certificates
   ```

Now the ACME server is accessible on `https://<OpenBao Address>:8200/v1/charm-acme/acme/directory`

Now you should be able to obtain a certificate from OpenBao using an ACME client, for example [Lego](https://go-acme.github.io/lego/).
