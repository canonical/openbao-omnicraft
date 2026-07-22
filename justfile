# Override the shared lint-grafana recipe: `go install module@version` fails because
# dashboard-linter v0.1.1's go.mod contains replace directives, so use the upstream
# prebuilt release binary instead (no Go toolchain required).
lint-grafana:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "linting Grafana dashboards..."

    arch="$(uname -m)"
    case "$arch" in
        x86_64) arch="amd64" ;;
        aarch64) arch="arm64" ;;
    esac
    tmpdir="$(mktemp -d)"
    curl -fsSL "https://github.com/grafana/dashboard-linter/releases/download/v0.1.1/dashboard-linter_0.1.1_linux_${arch}.tar.gz" \
        | tar -xz -C "$tmpdir"
    "$tmpdir/dashboard-linter" lint "$GRAFANA_DASHBOARD_FILE_PATH" --strict -c grafana.lint
    rm -rf "$tmpdir"

# Override the shared test-integration-charm recipe: our charm integration tests
# take the built artifact under test (the snap or the rock) via --resource-path.
[private]
test-integration-charm:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "running integration test for $CHARM_FILE_NAME"

    uv tool install tox --with tox-uv
    args=(--charm_path "$CHARM_FILE_NAME")
    if [ -n "${SNAP_FILE_NAME:-}" ]; then
        args+=(--resource-path "$SNAP_FILE_NAME")
    elif [ -n "${ROCK_FILE_NAME:-}" ]; then
        args+=(--resource-path "$ROCK_FILE_NAME")
    fi
    if [ -n "${KV_REQUIRER_CHARM_FILE_NAME:-}" ]; then
        args+=(--kv_requirer_charm_path "$KV_REQUIRER_CHARM_FILE_NAME")
    fi
    if [ -n "${TEST_MODULES:-}" ]; then
        # Run only the suites named in TEST_MODULES (space-separated module
        # names, with or without the .py suffix) by ignoring every other
        # module under tests/integration. tox always passes the whole
        # directory to pytest, so restriction must happen via --ignore.
        for f in tests/integration/test_*.py; do
            name="$(basename "$f" .py)"
            keep=false
            for m in ${TEST_MODULES}; do
                if [ "${m%.py}" = "$name" ]; then
                    keep=true
                fi
            done
            if [ "$keep" = false ]; then
                args+=(--ignore="$f")
            fi
        done
    fi
    tox -e integration -- "${args[@]}"

# Run custom tests for the rock
test-integration-rock:
    #!/usr/bin/env bash
    image_name="$(yq '.name' rockcraft.yaml)"
    echo "image_name=${image_name}" >> $GITHUB_ENV
    version="$(yq '.version' rockcraft.yaml)"
    echo "version=${version}" >> $GITHUB_ENV
    rock_file=$(ls *.rock | tail -n 1)

    sudo rockcraft.skopeo \
        --insecure-policy \
        copy \
        oci-archive:"${rock_file}" \
        docker-daemon:"${image_name}-rock:test"
    pip install tox
    cd tests && tox -e integration

# Run custom tests for the snap
test-integration-snap:
    #!/usr/bin/env bash
    SNAP_NAME=$(ls *.snap)
    echo "Installing snap $SNAP_NAME"
    sudo snap install --dangerous $SNAP_NAME

    sudo snap start openbao.server
    sleep 30

    sudo apt-get update
    sudo apt-get install -y net-tools
    # Check if a service is listening on port 8200
    if netstat -tuln | grep ':8200'; then
    echo "Service is listening on port 8200"
    else
    echo "Service is NOT listening on port 8200"
    exit 1
    fi
