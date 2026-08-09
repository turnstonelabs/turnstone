{{/*
Expand the name of the chart.
*/}}
{{- define "turnstone.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this
(by the DNS naming spec). If release name contains chart name it will be used
as a full name.
*/}}
{{- define "turnstone.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "turnstone.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "turnstone.labels" -}}
helm.sh/chart: {{ include "turnstone.chart" . }}
{{ include "turnstone.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "turnstone.selectorLabels" -}}
app.kubernetes.io/name: {{ include "turnstone.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use.
*/}}
{{- define "turnstone.serviceAccountName" -}}
{{- if .Values.serviceAccount }}
{{- if .Values.serviceAccount.name }}
{{- .Values.serviceAccount.name }}
{{- else }}
{{- include "turnstone.fullname" . }}
{{- end }}
{{- else }}
{{- include "turnstone.fullname" . }}
{{- end }}
{{- end }}

{{/*
Determine the PostgreSQL host.
*/}}
{{- define "turnstone.postgresql.host" -}}
{{- if .Values.postgresql.enabled }}
{{- printf "%s-postgresql" .Release.Name }}
{{- else }}
{{- .Values.database.external.host }}
{{- end }}
{{- end }}

{{/*
Determine the PostgreSQL port.
*/}}
{{- define "turnstone.postgresql.port" -}}
{{- if .Values.postgresql.enabled }}
{{- printf "5432" }}
{{- else }}
{{- .Values.database.external.port | toString }}
{{- end }}
{{- end }}

{{/*
Determine the PostgreSQL database name.
*/}}
{{- define "turnstone.postgresql.database" -}}
{{- if .Values.postgresql.enabled }}
{{- .Values.postgresql.auth.database }}
{{- else }}
{{- .Values.database.external.database }}
{{- end }}
{{- end }}

{{/*
Determine the PostgreSQL username.
*/}}
{{- define "turnstone.postgresql.username" -}}
{{- if .Values.postgresql.enabled }}
{{- .Values.postgresql.auth.username }}
{{- else }}
{{- .Values.database.external.username }}
{{- end }}
{{- end }}

{{/*
The PostgreSQL password when the chart stores it itself, empty when it
does not. Doubles as the predicate for "does <fullname>-secrets need to
carry POSTGRES_PASSWORD", so an inline password is never written
anywhere but <fullname>-secrets, and an operator-supplied Secret is
never duplicated into it.

An operator-supplied existingSecret wins outright: writing the value
into a second Secret nothing reads would only duplicate a credential.

Both branches need "default" because this is reached through include,
which captures rendered text rather than a value: a key that is unset
rather than empty — "password:" with nothing after it — renders as the
literal "<no value>", and a ten-character string is truthy. Without the
default that lands base64-encoded in POSTGRES_PASSWORD and the workloads
authenticate with it.
*/}}
{{- define "turnstone.db.inlinePassword" -}}
{{- if .Values.postgresql.enabled }}
{{- .Values.postgresql.auth.password | default "" }}
{{- else if not .Values.database.external.existingSecret }}
{{- .Values.database.external.password | default "" }}
{{- end }}
{{- end }}

{{/*
The name of the bundled subchart's own Secret.

Mirrors the subchart's naming rather than calling its helpers, which
expect a context scoped to the subchart that this chart cannot hand
them. Release-derived, so deliberately not turnstone.fullname: a
fullnameOverride here renames this chart's resources and leaves the
subchart's alone, and pointing at "<fullname>-postgresql" would then
name a Secret that does not exist.

The subchart also normalises the release name through a regex before
using it, which is a no-op for the DNS-1123 names Helm accepts, so it is
not reproduced.
*/}}
{{- define "turnstone.postgresql.fullname" -}}
{{- $global := ((.Values.global).postgresql).fullnameOverride }}
{{- if $global }}
{{- $global | trunc 63 | trimSuffix "-" }}
{{- else if .Values.postgresql.fullnameOverride }}
{{- .Values.postgresql.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := .Values.postgresql.nameOverride | default "postgresql" }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "turnstone.postgresql.secretName" -}}
{{- $existing := coalesce (((.Values.global).postgresql).auth).existingSecret .Values.postgresql.auth.existingSecret }}
{{- if $existing }}
{{- tpl $existing . }}
{{- else }}
{{- include "turnstone.postgresql.fullname" . }}
{{- end }}
{{- end }}

{{/*
The subchart stores the named user's password under "password" and the
superuser's under "postgres-password", and lets an operator rename
either through auth.secretKeys.
*/}}
{{- define "turnstone.postgresql.passwordKey" -}}
{{- $user := .Values.postgresql.auth.username | default "" }}
{{- $keys := .Values.postgresql.auth.secretKeys | default dict }}
{{- if or (empty $user) (eq $user "postgres") }}
{{- $keys.adminPasswordKey | default "postgres-password" }}
{{- else }}
{{- $keys.userPasswordKey | default "password" }}
{{- end }}
{{- end }}

{{/*
Determine the secret holding the PostgreSQL password, and the key within
it. Three sources, and the two helpers agree by construction because
they branch identically:

  - an external database pointed at a Secret the chart does not own (a
    CloudNativePG-generated secret, an External Secrets target, ...), in
    which case the key is rarely "POSTGRES_PASSWORD" — hence the
    companion existingSecretPasswordKey
  - the bundled subchart's own Secret, when it generates the password
  - <fullname>-secrets, when the password is supplied inline in values

Note the last is deliberately not turnstone.llm.secretName: that
resolves to llm.existingSecret when the operator supplies one, which
holds LLM API keys and has no reason to carry a database password.
*/}}
{{- define "turnstone.db.secretName" -}}
{{- if not .Values.postgresql.enabled }}
{{- if .Values.database.external.existingSecret }}
{{- .Values.database.external.existingSecret }}
{{- else }}
{{- printf "%s-secrets" (include "turnstone.fullname" .) }}
{{- end }}
{{- else if include "turnstone.db.inlinePassword" . }}
{{- printf "%s-secrets" (include "turnstone.fullname" .) }}
{{- else }}
{{- include "turnstone.postgresql.secretName" . }}
{{- end }}
{{- end }}

{{- define "turnstone.db.passwordKey" -}}
{{- if not .Values.postgresql.enabled }}
{{- if .Values.database.external.existingSecret }}
{{- .Values.database.external.existingSecretPasswordKey | default "password" }}
{{- else }}
{{- printf "POSTGRES_PASSWORD" }}
{{- end }}
{{- else if include "turnstone.db.inlinePassword" . }}
{{- printf "POSTGRES_PASSWORD" }}
{{- else }}
{{- include "turnstone.postgresql.passwordKey" . }}
{{- end }}
{{- end }}

{{/*
Database environment shared by the server, console and migrate Job.

Every value except the password is rendered inline rather than pulled
from the ConfigMap via envFrom, so that one definition serves all three
workloads and the URL is assembled in exactly one place.

POSTGRES_PASSWORD must still precede TURNSTONE_DB_URL: the kubelet
expands $(VAR) only against env entries declared earlier in the list, so
a later definition would leave a literal "$(POSTGRES_PASSWORD)" in the
URL.
*/}}
{{- define "turnstone.db.env" -}}
- name: TURNSTONE_DB_BACKEND
  value: {{ .Values.database.backend | quote }}
{{- if eq .Values.database.backend "sqlite" }}
{{- /*
  Set outright rather than left to default. The backend falls back to
  ".turnstone.db in cwd", which is only the mount because the image's
  final WORKDIR happens to be /data — a coincidence that would put the
  database on the root filesystem, unwritable and unpersisted, the day
  anything overrides the working directory.
*/}}
- name: TURNSTONE_DB_PATH
  value: {{ printf "%s/.turnstone.db" (include "turnstone.dataMountPath" .) | quote }}
{{- else }}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "turnstone.db.secretName" . }}
      key: {{ include "turnstone.db.passwordKey" . }}
- name: TURNSTONE_DB_URL
  value: "postgresql+psycopg://{{ include "turnstone.postgresql.username" . }}:$(POSTGRES_PASSWORD)@{{ include "turnstone.postgresql.host" . }}:{{ include "turnstone.postgresql.port" . }}/{{ include "turnstone.postgresql.database" . }}{{ if and (not .Values.postgresql.enabled) .Values.database.external.sslmode }}?sslmode={{ .Values.database.external.sslmode }}{{ end }}"
{{- end }}
{{- end }}

{{/*
The working directory, and on SQLite the database directory with it.

Fixed rather than configurable: it is the image's WORKDIR, so moving it
would desynchronise the mount from the process's cwd for no gain.
*/}}
{{- define "turnstone.dataMountPath" -}}
/data
{{- end }}

{{- define "turnstone.data.claimName" -}}
{{- if .Values.database.persistence.existingClaim }}
{{- .Values.database.persistence.existingClaim }}
{{- else }}
{{- printf "%s-data" (include "turnstone.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Is /data holding a database, as opposed to being scratch space?

Only on SQLite. On PostgreSQL every durable byte lives in the database
server and /data holds nothing worth keeping across a restart, so an
emptyDir is not a compromise there — it is the accurate description.
*/}}
{{- define "turnstone.data.persistent" -}}
{{- if and (eq .Values.database.backend "sqlite") .Values.database.persistence.enabled }}true{{ end }}
{{- end }}

{{/*
The /data volume and its mount. Every workload mounts it: it is the
working directory in all three, and on SQLite all three open the same
database file through it.
*/}}
{{- define "turnstone.dataVolume" -}}
- name: data
{{- if include "turnstone.data.persistent" . }}
  persistentVolumeClaim:
    claimName: {{ include "turnstone.data.claimName" . }}
{{- else }}
  emptyDir: {}
{{- end }}
{{- end }}

{{- define "turnstone.dataVolumeMount" -}}
- name: data
  mountPath: {{ include "turnstone.dataMountPath" . | quote }}
{{- end }}

{{/*
Reject the backend/topology combinations that cannot work, at render
time rather than as a pod that comes up wrong.

SQLite is a file opened by local processes, and this chart spreads those
processes across pods: the server and the console are separate
Deployments that both read and write the registry. WAL mode puts the
index in a mmap'd -shm file that only coheres between processes on one
host, so every consumer has to land on the node holding the claim. Two
server replicas cannot satisfy that — nor could they discover each
other, since discovery is itself rows in that same database.

Co-location is left to the operator's nodeSelector/affinity rather than
enforced here: the chart cannot tell which node the claim will bind to,
and inventing an affinity rule would collide with whatever placement the
operator already configured.
*/}}
{{- define "turnstone.validateBackend" -}}
{{- $backend := .Values.database.backend }}
{{- if not (has $backend (list "sqlite" "postgresql")) }}
{{- fail (printf "database.backend must be \"sqlite\" or \"postgresql\", got %q" $backend) }}
{{- end }}
{{- if eq $backend "sqlite" }}
{{- if .Values.postgresql.enabled }}
{{- fail "database.backend=sqlite conflicts with postgresql.enabled=true: set postgresql.enabled=false, or switch the backend to postgresql." }}
{{- end }}
{{- if gt (int .Values.server.replicas) 1 }}
{{- fail (printf "database.backend=sqlite supports one server replica, got %d: node discovery is rows in the shared database, which SQLite cannot be across pods. Use the postgresql backend to run more than one." (int .Values.server.replicas)) }}
{{- end }}
{{- if not .Values.database.persistence.enabled }}
{{- fail "database.backend=sqlite with database.persistence.enabled=false would put the database on an emptyDir and lose it on every restart. Enable persistence, or switch the backend to postgresql." }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Ephemeral writable volumes, and their mounts, for a workload's
writablePaths list. A read-only root filesystem leaves the container with
nowhere to write, and the image needs several such places: /tmp for
tempfile (skill scratch dirs, background shell scripts, generated TLS
material), the home directory for ~/.config/turnstone and the caches the
bundled git and npx tooling keeps, /data because it is the working
directory, and /workspace for the agent workspace.

Backed by emptyDir rather than the container's writable layer, which is
exactly what readOnlyRootFilesystem removes. Nothing here survives a
restart, but nothing here did before either.

Both halves iterate the same list so a path can never be mounted without
a volume behind it. Scope is the list itself, not the root context.
*/}}
{{- define "turnstone.writableVolumes" -}}
{{- range . }}
- name: {{ .name }}
  emptyDir: {{ if .sizeLimit }}{ sizeLimit: {{ .sizeLimit | quote }} }{{ else }}{}{{ end }}
{{- end }}
{{- end }}

{{- define "turnstone.writableVolumeMounts" -}}
{{- range . }}
- name: {{ .name }}
  mountPath: {{ .path | quote }}
{{- end }}
{{- end }}

{{/*
Determine the secret name for LLM API keys.
*/}}
{{- define "turnstone.llm.secretName" -}}
{{- if .Values.llm.existingSecret }}
{{- .Values.llm.existingSecret }}
{{- else }}
{{- printf "%s-secrets" (include "turnstone.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Determine the secret name for auth tokens.
*/}}
{{- define "turnstone.auth.secretName" -}}
{{- if .Values.auth.existingSecret }}
{{- .Values.auth.existingSecret }}
{{- else }}
{{- printf "%s-secrets" (include "turnstone.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Container image reference.
*/}}
{{- define "turnstone.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}
