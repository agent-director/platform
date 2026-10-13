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
{{- $name := default .Chart.Name .Values.nameOverride }}
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
{{- $name := include "platform.temporalName" . -}}
{{- $fullname := ternary .Release.Name (printf "%s-%s" .Release.Name $name) (contains $name .Release.Name) -}}
{{/* Namespace hook uses an untruncated -namespace suffix (10 chars). */}}
{{- default $fullname .Values.temporal.fullnameOverride | trunc 53 | trimSuffix "-" -}}
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

{{/* Preserve extra volumes through Helm list replacement; reject ambiguous collisions. */}}
{{- define "platform.temporalVolumes" -}}
{{- $trust := dict "name" "db-trust" "configMap" (dict "name" (include "platform.databaseTrust" .root) "items" (list (dict "key" (include "platform.databaseCAKey" .root) "path" "ca.crt"))) -}}
{{- $scratch := dict "name" "scratch" "emptyDir" (dict "sizeLimit" "1Gi") -}}
{{- $volumes := list $trust $scratch -}}
{{- $names := dict "db-trust" $trust "scratch" $scratch -}}
{{- $reserved := list "db-trust" "scratch" -}}
{{- if eq .component "server" -}}
{{- $runtimeConfig := dict "name" "server-runtime-config" "emptyDir" (dict "sizeLimit" "128Mi") -}}
{{- $volumes = append $volumes $runtimeConfig -}}
{{- $_ := set $names "server-runtime-config" $runtimeConfig -}}
{{- $reserved = append $reserved "server-runtime-config" -}}
{{- end -}}
{{- $builtIns := ternary (list "config" "dynamic-config" "shims" "internode-tls" "frontend-tls") (list "shims" "client-tls") (eq .component "server") -}}
{{- $inputNames := dict -}}
{{- range (index .root.Values.temporal .component).additionalVolumes -}}
{{- if has .name $builtIns -}}{{ fail (printf "Temporal volume %s collides with the pinned dependency" .name) }}{{- end -}}
{{- if hasKey $inputNames .name -}}{{ fail (printf "Temporal volume %s is duplicated" .name) }}{{- end -}}
{{- $_ := set $inputNames .name true -}}
{{- if hasKey $names .name -}}
{{- if or (not (has .name $reserved)) (not (deepEqual (get $names .name) .)) -}}{{ fail (printf "Temporal volume %s is duplicated or conflicts with the lifecycle projection" .name) }}{{- end -}}
{{- else -}}
{{- $_ := set $names .name . -}}
{{- $volumes = append $volumes . -}}
{{- end -}}
{{- end -}}
{{- toYaml $volumes -}}
{{- end -}}
{{- define "platform.temporalVolumeMounts" -}}
{{- $trust := dict "name" "db-trust" "mountPath" "/etc/db-trust" "readOnly" true -}}
{{- $scratch := dict "name" "scratch" "mountPath" "/tmp" -}}
{{- $mounts := list $trust $scratch -}}
{{- $paths := dict "/etc/db-trust" $trust "/tmp" $scratch -}}
{{- $reserved := list "/etc/db-trust" "/tmp" -}}
{{- $builtIns := list -}}
{{- if eq .component "server" -}}
{{- $runtimeConfig := dict "name" "server-runtime-config" "mountPath" "/etc/temporal/config" -}}
{{- $mounts = append $mounts $runtimeConfig -}}
{{- $_ := set $paths "/etc/temporal/config" $runtimeConfig -}}
{{- $reserved = append $reserved "/etc/temporal/config" -}}
{{- $builtIns = list "/etc/temporal/config/config_template.yaml" "/etc/temporal/config/docker.yaml" "/etc/temporal/dynamic_config" "/usr/local/bin/dockerize" -}}
{{- $tls := .root.Values.temporal.server.tls | default dict -}}
{{- with $tls.internode -}}{{- if .enabled -}}{{- $builtIns = append $builtIns .mountPath -}}{{- end -}}{{- end -}}
{{- with $tls.frontend -}}{{- if .enabled -}}{{- $builtIns = append $builtIns .mountPath -}}{{- end -}}{{- end -}}
{{- else -}}
{{- $builtIns = list "/usr/local/sbin/temporal-elasticsearch-tool" "/etc/temporal/tls/client" -}}
{{- end -}}
{{- $protected := concat (list "/etc/db-trust") $builtIns -}}
{{- if eq .component "server" -}}{{- $protected = append $protected "/etc/temporal/config" -}}{{- end -}}
{{- $inputPaths := dict -}}
{{- range (index .root.Values.temporal .component).additionalVolumeMounts -}}
{{- $path := clean .mountPath -}}
{{- if or (not (hasPrefix "/" $path)) (ne .mountPath $path) -}}{{ fail (printf "Temporal mount %s must be an absolute normalized path" .mountPath) }}{{- end -}}
{{- if not (hasKey $paths $path) -}}
{{- range $target := $protected -}}
{{- if or (eq $path $target) (hasPrefix (printf "%s/" (trimSuffix "/" $target)) $path) (hasPrefix (printf "%s/" (trimSuffix "/" $path)) $target) -}}
{{- fail (printf "Temporal mount %s shadows protected lifecycle/dependency path %s" $path $target) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if has .mountPath $builtIns -}}{{ fail (printf "Temporal mount %s collides with the pinned dependency" .mountPath) }}{{- end -}}
{{- if hasKey $inputPaths .mountPath -}}{{ fail (printf "Temporal mount %s is duplicated" .mountPath) }}{{- end -}}
{{- $_ := set $inputPaths .mountPath true -}}
{{- if hasKey $paths .mountPath -}}
{{- if or (not (has .mountPath $reserved)) (not (deepEqual (get $paths .mountPath) .)) -}}{{ fail (printf "Temporal mount %s is duplicated or conflicts with the lifecycle projection" .mountPath) }}{{- end -}}
{{- else -}}
{{- $_ := set $paths .mountPath . -}}
{{- $mounts = append $mounts . -}}
{{- end -}}
{{- end -}}
{{- toYaml $mounts -}}
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
