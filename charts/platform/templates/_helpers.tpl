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
{{- $name := include "platform.temporalName" . -}}
{{- $base := default .Release.Name .Values.fullnameOverride -}}
{{- $fullname := ternary $base (printf "%s-%s" $base $name) (contains $name $base) -}}
{{/* Headless membership Services append -matching-headless (18 chars). */}}
{{- $fullname | trunc 45 | trimSuffix "-" -}}
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
Generate a stable, collision-free Tailscale hostname.
*/}}
{{- define "platform.tailscaleHostname" -}}
{{- if .Values.caddyTailscale.hostname }}
{{- .Values.caddyTailscale.hostname }}
{{- else }}
{{- printf "%s-platform-%s" .Release.Namespace (trunc 4 (sha256sum .Release.Name)) }}
{{- end }}
{{- end }}
