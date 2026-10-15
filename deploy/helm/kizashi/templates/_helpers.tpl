{{/*
Expand the name of the chart.
*/}}
{{- define "kizashi.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "kizashi.fullname" -}}
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
Chart label.
*/}}
{{- define "kizashi.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "kizashi.labels" -}}
helm.sh/chart: {{ include "kizashi.chart" . }}
{{ include "kizashi.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "kizashi.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kizashi.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Container image reference.
*/}}
{{- define "kizashi.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}

{{/*
Name of the ConfigMap holding kizashi.yaml.
*/}}
{{- define "kizashi.configMapName" -}}
{{- printf "%s-config" (include "kizashi.fullname" .) }}
{{- end }}

{{/*
Name of the models PVC (or the user-supplied existing claim).
*/}}
{{- define "kizashi.modelsPvcName" -}}
{{- if .Values.models.persistence.existingClaim }}
{{- .Values.models.persistence.existingClaim }}
{{- else }}
{{- printf "%s-models" (include "kizashi.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Common volume + volumeMount blocks for config and models.
Included by both the StatefulSet and the CronJob.
*/}}
{{- define "kizashi.commonVolumes" -}}
- name: config
  configMap:
    name: {{ include "kizashi.configMapName" . }}
- name: models
  persistentVolumeClaim:
    claimName: {{ include "kizashi.modelsPvcName" . }}
- name: tmp
  emptyDir: {}
{{- if .Values.config.prometheus.bearerTokenSecretName }}
- name: bearer-token
  secret:
    secretName: {{ .Values.config.prometheus.bearerTokenSecretName }}
{{- end }}
{{- with .Values.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "kizashi.commonVolumeMounts" -}}
- name: config
  mountPath: /etc/kizashi
  readOnly: true
- name: models
  mountPath: /var/lib/kizashi/models
- name: tmp
  mountPath: /tmp
{{- if .Values.config.prometheus.bearerTokenSecretName }}
- name: bearer-token
  mountPath: /var/run/secrets/kizashi
  readOnly: true
{{- end }}
{{- with .Values.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Common env vars for both serve and train.
*/}}
{{- define "kizashi.commonEnv" -}}
- name: KIZASHI_LONG_TERM_URL
  value: {{ .Values.config.prometheus.longTermUrl | default .Values.config.prometheus.url | quote }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}
