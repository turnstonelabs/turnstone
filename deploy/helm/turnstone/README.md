# Turnstone Helm chart

Deploys the Turnstone server and console as two Deployments with Services
(ClusterIP by default), a database migration Job that runs on install and
before each upgrade, and, unless `postgresql.enabled` is false, a bundled
PostgreSQL. The chart needs Kubernetes 1.29 or newer.

## Install

The JWT signing secret is required. Create it once and name it, so that
upgrades and renders never change it:

```sh
kubectl create secret generic turnstone-auth \
  --from-literal=TURNSTONE_JWT_SECRET="$(openssl rand -hex 32)"
helm dependency build deploy/helm/turnstone
helm install turnstone deploy/helm/turnstone \
  --set auth.existingSecret=turnstone-auth
```

`auth.jwtSecret` takes the value inline instead; the render stops when neither
is set. A changed value, inline or in an existing Secret, needs
`kubectl rollout restart` of both Deployments so that every pod holds the
same secret. Either may hold a `secret://` reference, which the server and the
console resolve at startup once the configuration file below enables a secret
store, so the Secret then carries only the reference.

The notes printed after install say how to reach the server and the console.
`values.yaml` documents the settings; the ones most installs touch:

- `image.tag`: the image tag; empty means the chart's `appVersion`.
- `auth.existingSecret` (or `auth.jwtSecret`): the JWT signing secret; one
  is required.
- `database.external.*` and `postgresql.enabled`: use an existing PostgreSQL
  instead of the bundled one.
- `llm.apiKey` or `llm.existingSecret`: the fallback API key for model
  definitions that leave theirs empty.
- `server.replicas`: each replica is a node, and a session lives on one of
  them.
- `server.sidecars`: MCP servers in the server pod; see below.
- `config.file` (or `config.existingSecret`) and `config.secretMounts`: a
  configuration file and Secret mounts; see below.
- `ingress.*`: expose the server or the console.

## MCP servers

With this chart, run MCP servers in their own containers and connect to them
over Streamable HTTP. There are two places to put them.

### In their own Deployment

A server that talks to an API (a code host, a chat service, a tracker) needs
nothing from the Turnstone pods. Run it as its own Deployment and Service from
your own manifests, pin its image, and register its in-cluster URL in the
console under MCP Servers, Add Server, with transport `streamable-http`. Where
the server can require a bearer token, give it one and register it under
Static headers as `Authorization: Bearer <token>`. Such a server restarts and
upgrades on its own, and Turnstone reconnects to it.

### As a sidecar of the server pod

A server that works on the agent's files (a language toolchain, a test runner,
a version-control helper) has to see the same filesystem as the model's shell
and file tools. `server.sidecars` runs such servers in the server pod:

```yaml
server:
  sidecars:
    - name: toolchain
      image: registry.example.com/mcp/toolchain@sha256:...
      port: 9001
      env:
        - name: MCP_BEARER_TOKEN
          valueFrom:
            secretKeyRef:
              name: toolchain-mcp-token
              key: token
      resources:
        requests: {cpu: 50m, memory: 64Mi}
        limits: {cpu: 500m, memory: 256Mi}
      securityContext:
        runAsUser: 1000
        runAsNonRoot: true
        allowPrivilegeEscalation: false
        capabilities: {drop: [ALL]}
```

Each entry is a container spec, passed through as written, plus `port`, the
port the server listens on, and an optional `path` (default `/mcp`). An
entry's own `volumeMounts` may name only `workspace`, with a `subPath` for
example; the pod has no other volume for sidecars. The chart:

- mounts one shared `emptyDir` at `/workspace` in the server container and in
  every sidecar, the path the image already names in the model's tool
  descriptions;
- runs the entries as sidecars that are started before the server and stopped
  after it;
- stops automounting the service-account token and projects it into the
  server container alone; sidecars receive none of the chart's secret env
  either;
- refuses a missing or duplicated name, the name `server`, a port outside 1 to
  65535, a port used twice or equal to the server's, a `path` without a
  leading slash, and a mount of any volume but `workspace`;
- prints each sidecar's `http://localhost:<port><path>` in the install notes,
  to register in the console.

What to know before choosing a sidecar:

- **Streamable HTTP only.** Nothing in a pod lets the server start a process in
  another container. An image that only speaks stdio wraps a bridge in its own
  image: a small process that starts the stdio server and serves it over
  Streamable HTTP on the declared port.
- **Who can call it.** Every process in the pod can reach `localhost:<port>`,
  the model's own shell included, so a sidecar registered with No
  authorization can be called outside Turnstone's per-tool approvals. Only the
  shell tool's own checks apply to such a command (its approval, which may be
  automatic, and its blocklist), and the output guard to what comes back. A
  sidecar that listens on all interfaces also answers any pod in the cluster
  at the pod's IP unless a NetworkPolicy blocks it. Bind it to 127.0.0.1 where
  the image allows, and where it can require a bearer token, set one and
  register it under Static headers, as a `secret://` reference where the
  configuration file below enables them. The token keeps the sidecar from
  answering a stray request and, as a reference, out of the database; it is
  not a boundary against the model itself, whose shell runs in the server
  container. The approval gate on shell commands is that boundary, and no
  secret backend changes it.
- **Trust.** A sidecar shares the pod's network namespace, and its image is
  trusted like the server's. Run it as a non-root user with no added
  capabilities, as in the example. What the chart withholds from sidecars is
  the Kubernetes service-account token, the configuration file and Secret
  mounts below, and its own secret env; an identity granted to the pod outside
  the chart, through a cloud metadata endpoint for example, is reachable from
  every container.
- **Startup.** The server container starts once every sidecar's process is
  running, not once it listens; the server's MCP client retries until the
  tools appear. A `startupProbe` (`tcpSocket` on the port) holds the server
  back until a slow sidecar listens. A sidecar that cannot be pulled or
  started keeps the server on that pod from starting at all, so pin images
  you can always pull, and a sidecar `readinessProbe` gates the pod's
  readiness, and with it the node's Service endpoint.
- **Restarts.** A sidecar image change changes the pod template, so the server
  Deployment rolls and live sessions on that node are unloaded. The workspace
  `emptyDir` outlives a restart of the server container but not the pod, so
  the new pod starts with an empty workspace. Keep API-only servers in their
  own Deployment.
- **The console.** The localhost URL works only inside the server pod, so the
  console's own status row for such a server, and the connection test in the
  Add Server dialog, show an error; save without the test. That is expected:
  coordinators run in the console and have no node filesystem.
- **Files.** The server runs as user id 1000; a sidecar that changes the
  agent's files runs as the same user (`securityContext.runAsUser: 1000`), or
  files each side creates are writable only by their owner. The `emptyDir`
  itself is owned by root and writable by everyone, and git refuses a
  repository whose root it owns, so clone into a subdirectory such as
  `/workspace/<name>` rather than making `/workspace` itself a repository.
- **Resources.** Sidecar requests and limits count against the pod; set them.
- **Replicas.** Each server replica has its own workspace volume and its own
  sidecars. A session lives on one node, so this is consistent.

## Configuration file

The chart passes its own settings through env and command-line flags. For
settings it has no value for, `config.file` holds the text of a config.toml,
rendered into a Secret and mounted read-only at `/etc/turnstone/config.toml`
in the server and console pods; a changed file rolls both Deployments. The
chart refuses what it owns: the `[database]` connection keys, since a file
value would beat its env, and `[auth] jwt_secret`, which it takes from
`auth.jwtSecret` or `auth.existingSecret`; its port flags beat the file.
Because the file is a Secret, the startup secrets the chart cannot take
otherwise may live in it: the `[security]` encryption keys that per-user MCP
accounts need, the `[oidc]` `client_secret` for single sign-on, and a Vault
approle `secret_id`. Text the chart cannot parse as TOML is refused at
install; a file the application cannot read or parse, or a `[secrets]` table
it rejects, stops the pods at startup. Like
config.toml on any host, the file is readable by what runs in the server
container; the sidecar section above says where the boundary is.

To keep the file out of your values, put it in a Secret of your own under the
key `config.toml` and name it in `config.existingSecret` instead. The chart
mounts it the same way but does not look inside it: it refuses nothing, so
leave the `[database]` connection keys out of it, and it cannot roll the pods
when it changes, so a change needs `kubectl rollout restart` of both
Deployments. If the Secret or its `config.toml` key is missing, the pods wait
in ContainerCreating with a FailedMount event. Set `config.file` or
`config.existingSecret`, not both.

The pod templates carry a sha256 of `config.file` so that a changed text rolls
the pods. Anyone who can read pods can read it, and a weak secret in an
otherwise known file could be guessed from it, so use generated values. The
inline `auth.jwtSecret` is not hashed: a digest of the signing secret alone
would let the same readers guess a weak one offline, which is why a changed
secret needs the rollout restart described above.

The usual reason to use it is the `[secrets]` table, which lets a model's
`api_key`, an MCP server's static header, the JWT signing secret or the OIDC
client secret hold a `secret://` reference instead of the value (see
`docs/security.md`, "Secret References"). The table must be the same on the
console and every node, which the shared Secret gives you.

```yaml
config:
  file: |
    [secrets]
    cache_ttl_seconds = 300

    [secrets.file]
    root = "/run/turnstone/secrets"

    [secrets.vault]
    address = "https://vault.example.com:8200"
    auth = "jwt"
    auth_mount = "kubernetes"
    role = "turnstone"
    jwt_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    mount = "secret"
  secretMounts:
    - name: toolchain-mcp-token
      mountPath: /run/turnstone/secrets/toolchain-mcp-token
```

`secretMounts` mounts Kubernetes Secrets read-only into both pods at the path
given, each key a file; it needs a configuration file, and the Secrets must
exist before the pods start. Under the `[secrets.file] root`, each key's file
can be named by a reference. A reference must be the whole value, so a header
needs a key that holds the whole header:

```sh
kubectl create secret generic toolchain-mcp-token \
  --from-literal=token="$TOKEN" \
  --from-literal=authorization="Bearer $TOKEN"
```

The sidecar example reads `token`; in the console, register the header
`Authorization` as
`secret://file/run/turnstone/secrets/toolchain-mcp-token/authorization`.

Pick a dedicated directory for the root. Not `/run/secrets` or
`/var/run/secrets`: the pod's service-account token lives under them,
`/var/run` is `/run` in the image, and the application refuses such a root at
startup. A Vault approle credential can sit in the file as `secret_id`, or be
mounted the same way outside the root, at `/run/turnstone/vault` for example,
with `secret_id_file` pointing at the key's file there. The chart refuses a
mount that overlaps a path it reserves, the configuration directory, the
service-account token and the workspace, or another mount.

A reference keeps a value out of the database and out of what the console
shows. Like a sidecar's token, it is no boundary against the model's shell; the
sidecar section above says what is.

For Vault's Kubernetes auth method, the example's `jwt_path` is the pod's
service-account token: both pods automount it, and with sidecars the chart
projects it into the server container alone.
