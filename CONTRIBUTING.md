# Contributing

This repository contains the source code for the universal `openbao` charm, which deploys on both machine and Kubernetes models. You can find its contributing guidelines in [`charm/CONTRIBUTING.md`](charm/CONTRIBUTING.md).

Tool settings for linting, static analysis, and testing are configured in the repo-level `pyproject.toml`. Project-specific settings for `ruff` and `pyright` may be defined in individual `pyproject.toml` files if necessary, but prefer the repo-level `pyproject.toml` where possible.

## Documentation

Serve the documentation locally with:

```shell
cd docs
make run
```
