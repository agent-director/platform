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
