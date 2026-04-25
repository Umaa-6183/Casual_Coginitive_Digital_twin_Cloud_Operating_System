{{/*
═══════════════════════════════════════════════════════════════════════════════
CCDT Helm Chart — _helpers.tpl
Named template library used across all chart templates.
═══════════════════════════════════════════════════════════════════════════════
*/}}

{{/* ── Chart name ──────────────────────────────────────────────────────── */}}
{{- define "ccdt.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* ── Fully qualified app name ────────────────────────────────────────── */}}
{{- define "ccdt.fullname" -}}
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

{{/* ── Chart label ──────────────────────────────────────────────────────── */}}
{{- define "ccdt.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* ── Common labels (applied to every resource) ──────────────────────── */}}
{{- define "ccdt.labels" -}}
helm.sh/chart: {{ include "ccdt.chart" . }}
app.kubernetes.io/name:       {{ include "ccdt.name" . }}
app.kubernetes.io/instance:   {{ .Release.Name }}
app.kubernetes.io/version:    {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of:    ccdt-platform
{{- with .Values.global.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/* ── Selector labels (used in matchLabels — must be immutable) ──────── */}}
{{- define "ccdt.selectorLabels" -}}
app.kubernetes.io/name:     {{ include "ccdt.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* ── Component labels ─────────────────────────────────────────────────── */}}
{{- define "ccdt.componentLabels" -}}
{{- $component := index . 0 -}}
{{- $ctx      := index . 1 -}}
app:                              {{ $component }}
app.kubernetes.io/component:      {{ $component }}
app.kubernetes.io/name:           {{ $component }}
app.kubernetes.io/instance:       {{ $ctx.Release.Name }}
app.kubernetes.io/version:        {{ $ctx.Chart.AppVersion | quote }}
app.kubernetes.io/part-of:        ccdt-platform
app.kubernetes.io/managed-by:     {{ $ctx.Release.Service }}
helm.sh/chart:                    {{ include "ccdt.chart" $ctx }}
{{- with $ctx.Values.global.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/* ── Component selector labels ───────────────────────────────────────── */}}
{{- define "ccdt.componentSelectorLabels" -}}
{{- $component := index . 0 -}}
{{- $ctx      := index . 1 -}}
app: {{ $component }}
app.kubernetes.io/component: {{ $component }}
app.kubernetes.io/instance:  {{ $ctx.Release.Name }}
{{- end }}

{{/* ── Image reference helper ───────────────────────────────────────────── */}}
{{/* Usage: include "ccdt.image" (dict "ctx" . "repo" .Values.layer1.image "global" .Values.global) */}}
{{- define "ccdt.image" -}}
{{- $registry := .global.imageRegistry | trimSuffix "/" -}}
{{- $repo     := .repo.repository -}}
{{- $tag      := .repo.tag | default .global.imageTag -}}
{{- printf "%s/%s:%s" $registry $repo $tag -}}
{{- end }}

{{/* ── Namespace ───────────────────────────────────────────────────────── */}}
{{- define "ccdt.namespace" -}}
{{- default .Values.global.namespace .Release.Namespace -}}
{{- end }}

{{/* ── Pod security context (non-privileged layers) ──────────────────── */}}
{{- define "ccdt.podSecurityContext" -}}
{{- with .Values.podSecurityContext }}
{{- toYaml . }}
{{- end }}
{{- end }}

{{/* ── Container security context (non-privileged) ────────────────────── */}}
{{- define "ccdt.containerSecurityContext" -}}
{{- with .Values.containerSecurityContext }}
{{- toYaml . }}
{{- end }}
{{- end }}

{{/* ── Service Account name for a component ───────────────────────────── */}}
{{- define "ccdt.serviceAccountName" -}}
{{- printf "%s" . -}}
{{- end }}

{{/* ── Kafka bootstrap servers ─────────────────────────────────────────── */}}
{{- define "ccdt.kafkaBootstrap" -}}
{{- if .Values.kafka.enabled -}}
{{- printf "%s-kafka:9092" .Release.Name -}}
{{- else -}}
{{- .Values.global.kafkaBootstrap -}}
{{- end -}}
{{- end }}

{{/* ── Prometheus annotations ──────────────────────────────────────────── */}}
{{- define "ccdt.prometheusAnnotations" -}}
{{- $port := . -}}
prometheus.io/scrape: "true"
prometheus.io/port:   {{ $port | quote }}
prometheus.io/path:   "/metrics"
{{- end }}

{{/* ── Standard environment variables injected into every CCDT service ── */}}
{{- define "ccdt.commonEnv" -}}
- name: KAFKA_BOOTSTRAP_SERVERS
  valueFrom:
    secretKeyRef:
      name:     ccdt-kafka-credentials
      key:      bootstrap-servers
      optional: false
- name: LOG_LEVEL
  value: {{ .Values.global.logLevel | quote }}
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: POD_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: NODE_NAME
  valueFrom:
    fieldRef:
      fieldPath: spec.nodeName
{{- end }}

{{/* ── HPA spec helper ──────────────────────────────────────────────────── */}}
{{- define "ccdt.hpaMetrics" -}}
{{- $hpa := . -}}
metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type:               Utilization
        averageUtilization: {{ $hpa.targetCPUUtilizationPercentage }}
  {{- if $hpa.targetMemoryUtilizationPercentage }}
  - type: Resource
    resource:
      name: memory
      target:
        type:               Utilization
        averageUtilization: {{ $hpa.targetMemoryUtilizationPercentage }}
  {{- end }}
{{- end }}

{{/* ── Standard liveness probe (HTTP /health) ─────────────────────────── */}}
{{- define "ccdt.livenessProbe" -}}
{{- $port := index . 0 -}}
{{- $delay := index . 1 | default 30 -}}
livenessProbe:
  httpGet:
    path: /health
    port: {{ $port }}
  initialDelaySeconds: {{ $delay }}
  periodSeconds:       20
  failureThreshold:    3
  timeoutSeconds:      10
{{- end }}

{{/* ── Standard readiness probe (HTTP /health or /ready) ─────────────── */}}
{{- define "ccdt.readinessProbe" -}}
{{- $port := index . 0 -}}
{{- $delay := index . 1 | default 15 -}}
readinessProbe:
  httpGet:
    path: /health
    port: {{ $port }}
  initialDelaySeconds: {{ $delay }}
  periodSeconds:       10
  failureThreshold:    3
  timeoutSeconds:      5
{{- end }}

{{/* ── Tolerations for compute workloads ──────────────────────────────── */}}
{{- define "ccdt.computeTolerations" -}}
tolerations:
  - key:      "node-role"
    operator: "Equal"
    value:    "compute"
    effect:   "NoSchedule"
{{- end }}

{{/* ── Pod anti-affinity for HA deployments ───────────────────────────── */}}
{{- define "ccdt.podAntiAffinity" -}}
{{- $component := . -}}
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          labelSelector:
            matchLabels:
              app: {{ $component }}
          topologyKey: kubernetes.io/hostname
{{- end }}

{{/* ── Volume + VolumeMount for read-only model PVC ─────────────────────*/}}
{{- define "ccdt.modelVolume" -}}
{{- $name := index . 0 -}}
{{- $pvc  := index . 1 -}}
- name: model-storage
  persistentVolumeClaim:
    claimName: {{ if $pvc.existingClaim }}{{ $pvc.existingClaim }}{{ else }}{{ $name }}-model-pvc{{ end }}
    readOnly: true
{{- end }}

{{- define "ccdt.modelVolumeMount" -}}
- name:      model-storage
  mountPath: /app/checkpoints
  readOnly:  true
{{- end }}
