# ETP Explainer — Docker deployment

Production image for **Azure ML Custom Applications** (Azure AD in front) or private registry.

## Build

Requires GitHub SSH access to `rarko-arrive/arrive-ds` (private `arriveds` dep):

```bash
eval "$(ssh-agent -s)" && ssh-add   # once per session
./scripts/docker_build.sh           # → etp-explainer:local
# or:
DOCKER_BUILDKIT=1 docker build --ssh default -t etp-explainer:local .
```

Push to Azure Container Registry:

```bash
ACR=datasciencedev.azurecr.io   # your registry
az acr login -n "${ACR%%.azurecr.io}"
docker tag etp-explainer:local "$ACR/etp-explainer:latest"
docker push "$ACR/etp-explainer:latest"
```

## Local smoke test

```bash
DQT_DATA=/path/to/etp-dqt/data docker compose up --build
open http://127.0.0.1:8765
```

Optional lake mirror mount (fast uncached loads):

```yaml
# docker-compose.yml — uncomment mirror env + volume
DQT_USE_LAKE_MIRROR: "1"
DQT_LAKE_MIRROR: /mirror/etp_lake
```

## Azure ML Custom Application

Register on compute instance **rarko1** (workspace **datasciencedev**):

| Field | Value |
|---|---|
| Application name | `etp-explainer` |
| Docker image | `<acr>.azurecr.io/etp-explainer:latest` |
| Target port | `8765` |
| Published port | `8765` (range 8704–8993) |

**Environment variables:**

| Name | Value |
|---|---|
| `DQT_DATA_DIR` | `/data` |
| `EXPLAIN_CACHE_DIR` | `/cache` |
| `DQT_USE_LAKE_MIRROR` | `1` |
| `DQT_LAKE_MIRROR` | `/mirror/etp_lake` |

**Bind mounts:**

| Host (VM) | Container | Mode |
|---|---|---|
| `~/cloudfiles/.../etp-dqt/data` | `/data` | read-only |
| `/mnt/dqt/etp_lake` | `/mirror/etp_lake` | read-only |
| `/mnt/dqt/etp-explainer-cache` | `/cache` | read-write |

Teammates open the app from the compute **Applications** list — Azure AD gates access.

### Security model

- **Auth:** Azure ML / Entra ID (no app-level login in v1).
- **Container:** non-root (`app` uid 1000), read-only root FS, `no-new-privileges`.
- **Data:** lake parquet mounted read-only; only `/cache` is writable.
- **Network:** do not publish `0.0.0.0` on the host without AML — let Azure proxy the container port.

### VM prep (once per restart)

```bash
# etp-lake repo on VM
cd ~/cloudfiles/code/Users/rarko/dev/etp-lake
./scripts/vm_mirror_lake.sh --lite

mkdir -p /mnt/dqt/etp-explainer-cache
```

## Environment reference

| Variable | Default (container) | Purpose |
|---|---|---|
| `DQT_DATA_DIR` | `/data` | Lake + etp caches (read-only mount) |
| `EXPLAIN_CACHE_DIR` | `/cache` | Rendered HTML cache (writable) |
| `EXPLAIN_HOST` | `0.0.0.0` | Bind address inside container |
| `EXPLAIN_PORT` | `8765` | Listen port |
| `EXPLAIN_BEHIND_PROXY` | `1` | Trust `X-Forwarded-*` from AML proxy |
| `DQT_USE_LAKE_MIRROR` | unset | `1` → read parquet from mirror |
| `DQT_LAKE_MIRROR` | unset | e.g. `/mirror/etp_lake` |
