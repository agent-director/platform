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

{{/* Resource/Service/TLS contracts; all clients and certificates use these names. */}}
{{- define "agent-substrate.resourceName" -}}
{{- printf "%s-%s" (include "agent-substrate.fullname" .root | trunc (sub 62 (len .component) | int) | trimSuffix "-") .component -}}
{{- end -}}
{{- define "agent-substrate.apiService" -}}
{{- include "agent-substrate.resourceName" (dict "root" . "component" "ateapi") -}}
{{- end -}}
{{- define "agent-substrate.apiDNS" -}}
{{- printf "%s.%s.svc.cluster.local" (include "agent-substrate.apiService" .) .Release.Namespace -}}
{{- end -}}
{{- define "agent-substrate.apiPort" -}}
{{- .Values.ateapi.service.port | int -}}
{{- end -}}
{{- define "agent-substrate.tlsSecret" -}}
{{- default (include "agent-substrate.resourceName" (dict "root" . "component" "tls")) .Values.tls.secretName -}}
{{- end -}}
{{- define "agent-substrate.credentialSecret" -}}
{{- default (include "agent-substrate.tlsSecret" .) .Values.tls.existingBundleSecret -}}
{{- end -}}
{{/*
Resolve chart-managed TLS data once per render. Helm shares this chart's root
context across its templates; cache there, not in persisted/user-supplied Values.
The Secret and both rollout checksums must consume the same issued credentials.
*/}}
{{- define "agent-substrate.tlsData" -}}
{{- if not (hasKey . "_agentSubstrateTLSData") -}}
{{- $secretName := include "agent-substrate.tlsSecret" . }}
{{- $secret := lookup "v1" "Secret" .Release.Namespace $secretName }}
{{- $caCert := "" }}
{{- $tlsCert := "" }}
{{- $tlsKey := "" }}
{{- /* v0.3.0 credbundle.Parse accepts only PKCS#8 PRIVATE KEY blocks. Rotate
     legacy chart-owned PKCS#1/SEC1 credentials instead of retaining broken TLS. */}}
{{- if and $secret (hasPrefix (printf "-----BEGIN %s-----" "PRIVATE KEY") (index $secret.data "tls.key" | default "" | b64dec | trim)) }}
{{- /* Validate retained PKCS#8 DER rather than trusting the PEM label alone. */}}
{{- $retained := buildCustomCert (index $secret.data "tls.crt") (index $secret.data "tls.key") }}
{{- $caCert = index $secret.data "ca.crt" | b64dec }}
{{- $tlsCert = $retained.Cert }}
{{- $tlsKey = $retained.Key }}
{{- else }}
{{- $ca := genCA "agent-substrate-ca" 365 }}
{{- $service := include "agent-substrate.apiService" . }}
{{- /* Sprig emits RSA/EC keys in legacy formats; Ed25519 is emitted as PKCS#8. */}}
{{- $cert := genSignedCertWithKey $service nil (list $service (printf "%s.%s.svc" $service .Release.Namespace) (include "agent-substrate.apiDNS" .)) 365 $ca (genPrivateKey "ed25519") }}
{{- $caCert = $ca.Cert }}
{{- $tlsCert = $cert.Cert }}
{{- $tlsKey = $cert.Key }}
{{- end }}
{{- $data := dict
      "bundle.pem" (printf "%s\n%s\n%s" $tlsKey $tlsCert $caCert | b64enc)
      "ca.crt" ($caCert | b64enc)
      "tls.crt" ($tlsCert | b64enc)
      "tls.key" ($tlsKey | b64enc)
      "jwt.pem" ("{}" | b64enc)
      "empty.json" ("{}" | b64enc)
      "auth.yaml" ("actorIdentityJWTProvider: \"dummy\"\njwtProviders:\n- name: \"dummy\"\n  issuer: \"https://dummy\"\n  audiences: [\"dummy\"]\n" | b64enc) }}
{{- $_ := set . "_agentSubstrateTLSData" $data }}
{{- end -}}
{{- get . "_agentSubstrateTLSData" | toJson -}}
{{- end -}}

{{- define "agent-substrate.databaseCredential" -}}
{{- default (printf "%s.%s.credentials.postgresql.acid.zalan.do" (.Values.database.user | replace "_" "-") .Values.database.host) .Values.database.credentialSecret -}}
{{- end -}}
{{- define "agent-substrate.databaseDNS" -}}
{{- printf "%s.%s.svc.cluster.local" .Values.database.host .Values.platformNamespace -}}
{{- end -}}

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
{{- if $endpoints }}
{{- range $subset := $endpoints.subsets }}
{{- range $address := $subset.addresses }}
- ipBlock:
    cidr: {{ $address.ip }}/{{ ternary 128 32 (contains ":" $address.ip) }}
{{- end }}
{{- end }}
{{- end }}
{{- $svc := lookup "v1" "Service" "default" "kubernetes" }}
{{- if $svc }}
- ipBlock:
    cidr: {{ $svc.spec.clusterIP }}/{{ ternary 128 32 (contains ":" $svc.spec.clusterIP) }}
{{- end }}
{{- end }}
{{- end }}
