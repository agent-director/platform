{{/*
Expand the name of the chart.
*/}}
{{- define "platform.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "platform.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := include "platform.name" . }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/* Shared naming/port/DNS contracts. Component ports own both listeners and Services. */}}
{{- define "platform.resourceName" -}}
{{- $component := ternary "unified-api" .component (eq .component "unifiedApi") -}}
{{- printf "%s-%s" (include "platform.fullname" .root | trunc (sub 62 (len $component) | int) | trimSuffix "-") $component -}}
{{- end -}}
{{- define "platform.serviceName" -}}
{{- include "platform.resourceName" . -}}
{{- end -}}
{{- define "platform.serviceDNS" -}}
{{- printf "%s.%s.svc.cluster.local" (include "platform.serviceName" .) .root.Release.Namespace -}}
{{- end -}}
{{- define "platform.port" -}}
{{- index .root.Values .component "service" "port" | int -}}
{{- end -}}
{{- define "platform.httpURL" -}}
{{- printf "http://%s:%s" (include "platform.serviceDNS" .) (include "platform.port" .) -}}
{{- end -}}
{{- define "platform.selectorLabels" -}}
app: {{ .component }}
app.kubernetes.io/instance: {{ .root.Release.Name | quote }}
{{- end -}}
{{- define "platform.databaseName" -}}
{{- printf "%s-db" (.Release.Name | trunc 60 | trimSuffix "-") -}}
{{- end -}}
{{- define "platform.databaseDNS" -}}
{{- printf "%s.%s.svc.cluster.local" (include "platform.databaseName" .) .Release.Namespace -}}
{{- end -}}
{{- define "platform.databaseCredential" -}}
{{- printf "%s.%s.credentials.postgresql.acid.zalan.do" (.user | replace "_" "-") (include "platform.databaseName" .root) -}}
{{- end -}}
{{- define "platform.databaseTLSSecret" -}}
{{- printf "%s-tls" (include "platform.databaseName" .) -}}
{{- end -}}
{{- define "platform.databaseTrust" -}}
{{- printf "%s-trust" (include "platform.databaseName" .) -}}
{{- end -}}
{{- define "platform.databaseCAKey" -}}
{{- ternary "service-ca.crt" "ca.crt" .Values.global.tls.useOcpServingCert -}}
{{- end -}}
{{- define "platform.temporalSchemaServiceAccount" -}}
{{- include "platform.resourceName" (dict "root" . "component" "temporal-schema") -}}
{{- end -}}
{{- define "platform.temporalName" -}}
{{- default "temporal" .Values.temporal.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "platform.temporalFullname" -}}
{{- if .Values.temporal.fullnameOverride -}}
{{- .Values.temporal.fullnameOverride | trunc 45 | trimSuffix "-" -}}
{{- else -}}
{{- /*
A base that already contains the name is reused as is, so a release named "temporal" does not render
"temporal-temporal-*". It is never reused when it equals the UI fullname (a fullnameOverride, or a release
that also contains the chart name), because Temporal and UI "-frontend" objects would then collide.
Otherwise the base is truncated before "-<name>" is appended so the suffix always survives. The result is
at most 45 characters; headless membership Services append -matching-headless (18 chars).
*/ -}}
{{- $maxLength := 45 -}}
{{- $name := include "platform.temporalName" . | trunc 20 | trimSuffix "-" -}}
{{- $base := default .Release.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- if and (contains $name $base) (le (len $base) $maxLength) (ne $base (include "platform.fullname" .)) -}}
{{- $base -}}
{{- else -}}
{{- $baseBudget := sub $maxLength (add 1 (len $name)) | int -}}
{{- printf "%s-%s" ($base | trunc $baseBudget | trimSuffix "-") $name -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- define "platform.temporalService" -}}
{{- if .Values.temporalDev.enabled -}}
{{- include "platform.serviceName" (dict "root" . "component" "temporal-dev") -}}
{{- else -}}
{{- printf "%s-frontend" (include "platform.temporalFullname" .) -}}
{{- end -}}
{{- end -}}
{{- define "platform.temporalPort" -}}
{{- if .Values.temporalDev.enabled -}}{{ .Values.temporalDev.service.port | int }}
{{- else -}}{{ .Values.temporal.server.frontend.service.port | int }}{{- end -}}
{{- end -}}
{{- define "platform.temporalAddress" -}}
{{- printf "%s.%s.svc.cluster.local:%s" (include "platform.temporalService" .) .Release.Namespace (include "platform.temporalPort" .) -}}
{{- end -}}
{{/*
Temporal listener ports are fixed by the server image; only the frontend gRPC port is configurable.
Single source for temporal.yaml and the temporal NetworkPolicy. A port of 0 means the role does not
expose it; probePortName names the container port that the role's TCP probes target. JSON-encoded
because defines return strings: consume with `fromJsonArray` and wrap numbers in `int`.
*/}}
{{- define "platform.temporalRoles" -}}
{{- list
  (dict "name" "frontend" "rpcPort" (include "platform.temporalPort" . | int) "httpPort" 7243 "membershipPort" 6933 "probePortName" "rpc")
  (dict "name" "history" "rpcPort" 7234 "httpPort" 0 "membershipPort" 6934 "probePortName" "rpc")
  (dict "name" "matching" "rpcPort" 7235 "httpPort" 0 "membershipPort" 6935 "probePortName" "rpc")
  (dict "name" "worker" "rpcPort" 0 "httpPort" 0 "membershipPort" 6939 "probePortName" "membership")
  | toJson -}}
{{- end -}}
{{/* NetworkPolicy port entries (rpc ports, then membership ports) for Temporal peer traffic. Use: `| trim | nindent N`. */}}
{{- define "platform.temporalPeerPorts" -}}
{{- $roles := include "platform.temporalRoles" . | fromJsonArray -}}
{{- range $roles }}
{{- if gt (int .rpcPort) 0 }}
- protocol: TCP
  port: {{ int .rpcPort }}
{{- end }}
{{- end }}
{{- range $roles }}
- protocol: TCP
  port: {{ int .membershipPort }}
{{- end }}
{{- end -}}
{{/* Name/instance labels selecting every first-party Temporal pod. Arg: root context. */}}
{{- define "platform.temporalSelectorLabels" -}}
app.kubernetes.io/name: {{ include "platform.temporalName" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}
{{/* Label triple shared by every first-party Temporal object and its component selectors. Args: dict "root" $ "component" <string>. */}}
{{- define "platform.temporalLabels" -}}
{{ include "platform.temporalSelectorLabels" .root }}
app.kubernetes.io/component: {{ .component | quote }}
{{- end -}}
{{- define "platform.temporalPodSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}
{{- define "platform.temporalContainerSecurityContext" -}}
runAsNonRoot: true
readOnlyRootFilesystem: true
allowPrivilegeEscalation: false
capabilities:
  drop: [ALL]
{{- end -}}
{{- define "platform.temporalJobAnnotations" -}}
ignore-check.kube-linter.io/no-readiness-probe: "Schema/namespace setup are finite lifecycle Jobs; successful exit, not serving readiness, gates rollout."
ignore-check.kube-linter.io/no-liveness-probe: "Schema/namespace setup use bounded retries and the Helm timeout; container liveness probes are not meaningful for their completion markers."
{{- end -}}
{{/* db-trust + scratch volumes shared by the Temporal Deployments and Jobs. Arg: root context. */}}
{{- define "platform.temporalDbTrustVolumes" -}}
- name: db-trust
  configMap:
    name: {{ include "platform.databaseTrust" . }}
    items:
      - key: {{ include "platform.databaseCAKey" . }}
        path: ca.crt
- name: scratch
  emptyDir:
    sizeLimit: 1Gi
{{- end -}}
{{/* nodeSelector/affinity/tolerations for a pod spec. Arg: .Values.temporal.server or .Values.temporal.schema. Call via `with include ... | trim`. */}}
{{- define "platform.temporalScheduling" -}}
{{- with .nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/*
Generate a stable, collision-free Tailscale hostname.
*/}}
{{- define "platform.tailscaleHostname" -}}
{{- if .Values.caddyTailscale.hostname }}
{{- .Values.caddyTailscale.hostname }}
{{- else }}
{{- printf "%s-platform-%s" .Release.Namespace (trunc 4 (sha256sum .Release.Name)) }}
{{- end }}
{{- end }}
