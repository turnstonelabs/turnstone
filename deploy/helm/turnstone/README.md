# turnstone Helm chart

Deploys a Turnstone cluster: server nodes, the console (dashboard + routing
proxy), and optionally SearXNG (web_search backend), the channel gateway, and
a Karpenter disruption guard. Browsers reach the console only; it proxies
every server node.

This chart **ships no bundled PostgreSQL** (see `Chart.yaml` for why the
upstream Bitnami subchart was removed). Either let the chart create a
dedicated CloudNativePG `Cluster` (`cnpg.enabled`, operator required) or point
`database.external` at an existing PostgreSQL.

## Minimum values

```yaml
image:
  tag: "1.8.5"                       # pin; never a floating tag
cnpg:
  enabled: true                      # chart-managed CNPG Cluster <release>-db
  storage:
    storageClass: gp3
auth:
  existingSecret: turnstone-auth     # key TURNSTONE_JWT_SECRET
```

Without CNPG:

```yaml
database:
  external:
    host: my-postgres.example.svc
    existingSecret: my-db-secret     # key `password` (configurable)
```

## Out-of-band Secrets

| Secret | Keys | Used by |
|---|---|---|
| `auth.existingSecret` | `TURNSTONE_JWT_SECRET` | console, server, channel |
| `database.external.existingSecret` (only without `cnpg.enabled`) | `password` (key configurable) | console, server, channel, migrate Job. With `cnpg.enabled` CNPG's generated `<cluster>-app` Secret is used automatically. |
| `config.existingSecret` (optional) | `config.toml` | console, server (`TURNSTONE_CONFIG`); OIDC, `[security]` key, `[models.*]` |
| `channel.existingSecret` (optional) | `TURNSTONE_DISCORD_TOKEN`, `TURNSTONE_SLACK_TOKEN`, `TURNSTONE_SLACK_APP_TOKEN` | channel gateway |
| `llm.existingSecret` (optional) | `OPENAI_API_KEY` | fallback key for model definitions without one |

## Notable options

| Value | Why |
|---|---|
| `cnpg.enabled` | Creates a dedicated CloudNativePG Cluster in the release namespace; `database.external.host`/`existingSecret` default to it. |
| `server.workloadKind: StatefulSet` | Stable node ids (`…-server-N`) that survive restarts; required for Turnstone's "Specific node" affinity to be safe. Default `Deployment` matches upstream. |
| `server.nodeIdFromPodName` | Sets `TURNSTONE_NODE_ID` to the pod name. Without it each start mints `{hostname}_{4hex}`. |
| `httpRoute` | Gateway API route to the console (preferred over `ingress` when a shared Gateway exists). |
| `httpRoute.istioDenyPaths` | Istio `AuthorizationPolicy` on the parent Gateway denying `/metrics` and `/node/*/metrics` for this chart's hostnames (Turnstone serves `/metrics` unauthenticated). |
| `searxng.enabled` | Backs `web_search` for OpenAI-compatible / local models. The Service is named exactly `searxng` so Turnstone's default `tools.searxng_url` resolves with no configuration. |
| `disruptionGuard.enabled` | CronJob toggling `karpenter.sh/do-not-disrupt` on server pods by live workstream state. |
| `pdb.enabled`, `priorityClass.enabled` | One node drains at a time; non-preempting priority. |
| `config.existingSecret` | Mounts the shared bootstrap `config.toml` (see `docs/docker.md`, "Shared bootstrap config"). |

## Model definitions

The server reads models from the console **Models** tab (database) and from
`[models.*]` in `config.toml`; nothing is discovered from an endpoint except
the context window on vLLM-style servers. Through a gateway, set
`context_window` explicitly on each definition.

## Validation

```sh
helm lint .
helm template turnstone . -f <your-values.yaml> | kubectl apply --dry-run=server -f -
```
