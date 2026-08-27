# OpenBao snap — PKCS#11 troubleshooting

The OpenBao snap ships a **static** `bao` binary and an external **PKCS#11 KMS plugin**
(`openbao-plugin-kms-pkcs11`) on amd64/arm64. Strict confinement means host library
paths are not visible inside the snap. Most PKCS#11 failures we see come from that.

Config and env used by the server daemon:

| Path | Role |
|------|------|
| `/var/snap/openbao/common/openbao-config.hcl` | Server config (seal, plugin registration) |
| `/var/snap/openbao/common/openbao.env` | Sourced by `baod-start` (library path, vendor env) |
| `/snap/openbao/current/plugins/` | KMS plugin binary + `pkcs11.version` |

On first install, defaults are copied from the snap into `$SNAP_COMMON`. After that,
edit the copies under `/var/snap/openbao/common/` (refresh does not overwrite `openbao.env`
once it exists).

Also see the machine charm how-to: [Configure PKCS#11 HSM auto-unseal](../docs/how-to/configure_pkcs11_hsm.md).

## What is general vs vendor-specific

**Any PKCS#11 HSM under this snap**

- Register the KMS plugin (`plugin_directory` + `plugin "kms" "pkcs11"`).
- Put the PKCS#11 module under snap-common (not `/usr/lib/...`).
- Copy transitive shared libraries next to it (or rely on libs staged in the snap).
- Set `LD_LIBRARY_PATH` to include that directory (`openbao.env` / `baod-start`).
- Create the seal key on the HSM before `bao operator init`.
- Connect `raw-usb` / `hardware-observe` only if the device needs USB.

**YubiHSM only**

- Run `yubihsm-connector` and a `yubihsm_pkcs11.conf` with `connector = http://…`.
- Set `YUBIHSM_PKCS11_CONF` (defaulted in the snap’s `openbao.env`).
- Also ship `libyubihsm.so.2` and the HTTP/USB backend (`.so` needs **libcurl** or **libusb**).
- PKCS#11 PIN format: `{auth_key_id as 4 hex digits}{password}` (e.g. `0002S3cretPass`).
- Password must be at least **8** characters (total PIN length 12–68 bytes).

## Checklist before `init`

1. Plugin present: `ls /snap/openbao/current/plugins/openbao-plugin-kms-pkcs11`
2. Module visible inside the snap and deps resolve:
   ```bash
   snap run --shell openbao.server -c '
     . /var/snap/openbao/common/openbao.env
     ldd /var/snap/openbao/common/hsm-lib.so   # or your module path
   '
   ```
   Every line should resolve; no `not found`.
3. Seal stanza in `/var/snap/openbao/common/openbao-config.hcl` with `lib` under snap-common.
4. For YubiHSM: connector `status=OK`, conf file exists, PIN uses `0002…` style.
5. Restart: `sudo snap restart openbao.server`
6. Initialize: `bao operator init` (expect **recovery keys**, not Shamir unseal keys).

## Common errors

### `this build of OpenBao has PKCS#11 disabled`

The main `bao` binary is static (CGO off). You need the external KMS plugin stanza in
config (amd64/arm64 snap that ships `plugins/openbao-plugin-kms-pkcs11`).

### Panic / nil pointer in `miekg/pkcs11` (`Initialize`)

`dlopen` of the seal `lib` failed. Typical causes:

- `lib` points at a host path (`/usr/lib64/...`) that the snap cannot see.
- The file is missing under `/var/snap/openbao/common/`.
- A direct dependency of the module is missing (`ldd` shows `not found`).

**Fix:** copy the module into `$SNAP_COMMON`, point `lib` there, copy missing deps into
`$SNAP_COMMON` or `$SNAP_COMMON/hsm`, ensure `LD_LIBRARY_PATH` includes that directory,
restart the server.

### `CKR_ARGUMENTS_BAD` during initialize (not login)

Often the YubiHSM PKCS#11 module has no usable config. Create:

```bash
sudo tee /var/snap/openbao/common/yubihsm_pkcs11.conf >/dev/null <<'EOF'
connector = http://127.0.0.1:12345
EOF
```

Ensure `YUBIHSM_PKCS11_CONF` points at that file (`openbao.env`) and the connector is up:

```bash
curl -sS http://127.0.0.1:12345/connector/status
```

### `CKR_FUNCTION_FAILED` during initialize

Config was found, but a backend failed to load. For YubiHSM over HTTP, check:

```bash
snap run --shell openbao.server -c '
  . /var/snap/openbao/common/openbao.env
  ldd /var/snap/openbao/common/libyubihsm_http.so.2
'
```

`libcurl.so.4 => not found` (or similar) means copy those libraries into `$SNAP_COMMON`
(or rebuild a snap that stages `libcurl` / `libusb`). USB backends need `libusb`.

### `CKR_ARGUMENTS_BAD` during login (`failed to login`)

For YubiHSM this is almost always PIN format or length:

```text
pin = "{4-hex-auth-key-id}{password}"   # e.g. 0002S3cretPass
```

- Password must be **≥ 8** characters.
- Total PIN length must be **12–68** bytes.

Bare passwords like `S3cret` (6 chars) or `0002S3cret` (10 bytes total) fail login.

### `stored unseal keys … none were found` / `security barrier not initialized`

The seal path is working; the server is simply not initialized yet:

```bash
export BAO_ADDR=http://127.0.0.1:8200
bao operator init
```

Store the recovery keys and root token.

### USB plugs disconnected

```bash
sudo snap connect openbao:raw-usb
sudo snap connect openbao:hardware-observe
```

Needed when the PKCS#11 stack talks to a local USB device (not when only using a
remote/network connector).

### Mixing YubiHSM package versions

Ubuntu packages (`libyubihsm2`, shell 2.7.1) **conflict** with Yubico 2.7.3 packages
(`libyubihsm1`). Keep one consistent SDK stack; do not mix. After changing the PKCS#11
`.so` on the host, recopy it (and deps) into `$SNAP_COMMON`.

## Useful commands

```bash
# Service logs
journalctl -u snap.openbao.server.service -e

# Env and libraries as the daemon sees them
snap run --shell openbao.server -c '
  . /var/snap/openbao/common/openbao.env
  echo "YUBIHSM_PKCS11_CONF=$YUBIHSM_PKCS11_CONF"
  echo "LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
  ls -la /var/snap/openbao/common/
  ldd /var/snap/openbao/common/hsm-lib.so
'

# Plugin checksum (must match sha256sum in openbao-config.hcl)
sha256sum /snap/openbao/current/plugins/openbao-plugin-kms-pkcs11

# YubiHSM PKCS#11 debug (noisy)
# Add to /var/snap/openbao/common/openbao.env:
#   export YUBIHSM_PKCS11_DBG=1
# then: sudo snap restart openbao.server
```
