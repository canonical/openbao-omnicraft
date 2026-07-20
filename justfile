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

    sudo snap start bao
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
