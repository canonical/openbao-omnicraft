# Production blueprint (machine)

This document outlines recommendations for operating OpenBao in a production environment. 

```{image} ../images/production_blueprint_machine.png
:alt: Production Blueprint
:align: center
```

## Infrastructure

Please follow the [OpenBao project reference](https://developer.hashicorp.com/vault/tutorials/day-one-raft/raft-reference-architecture#hardware-sizing-for-vault-servers) to deploy the OpenBao charms on hosts of appropriate size for your deployment.

## High Availability

OpenBao should be deployed with a total of **5 units**.

## Observability

OpenBao should be integrated with Canonical Observability Stack:
- OpenBao should be integrated with Grafana Agent using the `cos-agent` charm relation interface.
- Grafana Agent should be integrated with COS using the `loki_push_api`, `prometheus_remote_write`, and `grafana_dashboard` charm relation interfaces.

## Backup and Restore

OpenBao should be integrated with an S3 provider to conduct regular backup operations.
