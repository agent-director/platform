{{/*
Expand the name of the chart.
*/}}
{{- define "agent-substrate.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "agent-substrate.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "agent-substrate.labels" -}}
helm.sh/chart: {{ include "agent-substrate.chart" . }}
{{ include "agent-substrate.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "agent-substrate.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agent-substrate.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "agent-substrate.fullname" -}}
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
Dynamically resolve Kubernetes API Server Egress CIDRs
*/}}
{{- define "agent-substrate.apiServerEgress" -}}
{{- if .Values.global.apiServerCIDRs }}
{{- range .Values.global.apiServerCIDRs }}
- ipBlock:
    cidr: {{ . }}
{{- end }}
{{- else }}
{{- $endpoints := lookup "v1" "Endpoints" "default" "kubernetes" }}
{{- if and $endpoints $endpoints.subsets }}
{{- range $subset := $endpoints.subsets }}
{{- range $address := $subset.addresses }}
- ipBlock:
    cidr: {{ $address.ip }}/32
{{- end }}
{{- end }}
{{- end }}
{{- $svc := lookup "v1" "Service" "default" "kubernetes" }}
{{- if $svc }}
- ipBlock:
    cidr: {{ $svc.spec.clusterIP }}/32
{{- end }}
{{- end }}
{{- end }}
