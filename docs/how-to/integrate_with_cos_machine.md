# Integrate with COS (Machine)

In this guide, we will cover how-to integrate OpenBao with Canonical Observability Stack (COS) for metrics and logs.

## Pre-requisites

* Juju >= 3.4

## 1. Deploy COS Lite

Create a Kubernetes model for observability:

```
juju add-model cos
```

Deploy cos lite and wait for all applications to be in active status:

```
juju deploy cos-lite --trust
```

Create offers for integrating with COS:
```
juju offer prometheus:receive-remote-write prometheus
juju offer loki:logging loki
juju offer grafana:grafana-dashboard grafana-dashboard
```

## 2. Integrate with COS

Switch to the machine model in which OpenBao is deployed:

```
juju switch <openbao model>
```

Deploy Opentelemetry Collector:

```
juju deploy opentelemetry-collector
```

Integrate OpenBao with Opentelemetry Collector:

```
juju relate openbao:cos-agent opentelemetry-collector:cos-agent
juju relate openbao:juju-info opentelemetry-collector:juju-info # for node exporter monitoring
```

Consume the COS offers:

```
juju consume cos.prometheus
juju consume cos.loki
juju consume cos.grafana
```

Integrate Opentelemetry Collector with COS:

```
juju relate prometheus:receive-remote-write opentelemetry-collector:send-remote-write
juju relate loki:logging opentelemetry-collector:send-loki-logs
juju relate grafana:grafana-dashboard opentelemetry-collector:grafana-dashboards-provider
```

## 3. Access OpenBao metrics and logs

Switch to the cos model:

```
juju switch cos
```

Retrieve the Grafana admin password:
```
juju run grafana/leader get-admin-password
```

Log in Grafana, and access metrics and logs.
