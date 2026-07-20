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

# The charm libraries are committed to this repository (lib/ and .vendored/),
# so there is nothing for charmcraft fetch-libs to do.
[private]
build-charm:
    #!/usr/bin/env bash
    set -euo pipefail

    charmcraft pack --verbose

# Publish every built charm (one file per architecture), not just the first.
[private]
publish-charm:
    #!/usr/bin/env bash
    set -euo pipefail

    channel="${CHARM_CHANNEL:-latest}/edge"
    charm_name=$(yq -r '.name' charmcraft.yaml)

    shopt -s nullglob
    charms=(*.charm)
    if [ ${#charms[@]} -eq 0 ]; then
        echo "::error::No charm file found to publish"
        exit 1
    fi

    for charm in "${charms[@]}"; do
        echo "Uploading $charm"
        revision=$(charmcraft upload "$charm" --format json | yq -r '.revision')
        echo "Releasing $charm_name revision $revision to $channel"
        charmcraft release "$charm_name" --revision "$revision" --channel "$channel"
    done

# Publish every built snap (one file per architecture), not just the first.
[private]
publish-snap:
    #!/usr/bin/env bash
    set -euo pipefail

    release_channel="${SNAP_TRACK:-latest}/edge"

    shopt -s nullglob
    snaps=(*.snap)
    if [ ${#snaps[@]} -eq 0 ]; then
        echo "::error::No snap file found to publish"
        exit 1
    fi

    for snap in "${snaps[@]}"; do
        echo "Publishing $snap to $release_channel"
        snapcraft upload "$snap" --release "$release_channel"
    done
