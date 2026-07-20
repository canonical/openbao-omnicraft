# OpenBao Omnicraft justfile

#

# In CI, common.just is merged into this file by the prepare action.

# For local development, install just (https://just.systems) and run:

# just --list

import? "https://raw.githubusercontent.com/canonical/identity-credentials-workflows/refs/tags/v3.1.3/common.just"

# Repo-specific override of the shared recipe: the openbao charms need extra
# test artifacts next to the charm under test (the workload snap for the
# machine charm and the kv-requirer test charm for both), and the arch
# integration jobs narrow the run with extra pytest arguments.
#
# Extra environment variables understood here on top of the shared ones:
#   SNAP_FILE_NAME                 passed as --snap_path (machine charm only)
#   KV_REQUIRER_CHARM_FILE_NAME    passed as --kv_requirer_charm_path
#   PYTEST_ADDOPTS_EXTRA           appended to the pytest invocation, e.g. "-k test_core.py"
[private]
test-integration-charm: install-python
    #!/usr/bin/env bash
    set -euo pipefail
    echo "running integration test for $CHARM_FILE_NAME"

    uv tool install tox --with tox-uv
    tox -e integration -- \
        --charm_path "$CHARM_FILE_NAME" \
        ${SNAP_FILE_NAME:+--snap_path "$SNAP_FILE_NAME"} \
        ${KV_REQUIRER_CHARM_FILE_NAME:+--kv_requirer_charm_path "$KV_REQUIRER_CHARM_FILE_NAME"} \
        ${PYTEST_ADDOPTS_EXTRA:-}
