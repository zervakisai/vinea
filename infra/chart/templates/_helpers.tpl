{{- define "vinea.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "vinea.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "vinea.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "vinea.labels" -}}
app.kubernetes.io/name: {{ include "vinea.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "vinea.appImage" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "vinea.uiImage" -}}
{{ .Values.uiImage.repository }}:{{ .Values.uiImage.tag | default .Chart.AppVersion }}
{{- end -}}

{{/*
Every container runs with the same hardened context. runAsUser matches the uid
baked into the image (1001): a mismatch here and the process cannot read its own
venv, which surfaces as a confusing ImportError rather than a permission error.
*/}}
{{- define "vinea.securityContext" -}}
runAsNonRoot: true
runAsUser: 1001
runAsGroup: 1001
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/*
Secrets by reference only -- see values.yaml. Non-secret config is plain env, so
`helm get values` stays readable without exposing anything.
*/}}
{{- define "vinea.env" -}}
envFrom:
  - secretRef:
      name: {{ .Values.secret.name }}
env:
  - name: VINEA_MODEL
    value: {{ .Values.config.model | quote }}
  - name: VINEA_PROMPT_LABEL
    value: {{ .Values.config.promptLabel | quote }}
{{- if .Values.gateway.enabled }}
  # The URL is the whole switch. Unset -> `resolve_model()` returns the plain model
  # string and the deployment behaves as though no gateway existed.
  # VINEA_GATEWAY_KEY is the virtual key and comes from the Secret via envFrom
  # above, never from here -- values are readable with `helm get values`.
  - name: VINEA_GATEWAY_URL
    value: {{ include "vinea.gatewayUrl" . | quote }}
  - name: VINEA_GATEWAY_MODEL
    value: {{ .Values.gateway.model | quote }}
{{- end }}
{{- end -}}

{{- define "vinea.gatewayUrl" -}}
{{- if .Values.gateway.externalUrl -}}
{{ .Values.gateway.externalUrl }}
{{- else -}}
http://{{ include "vinea.fullname" . }}-gateway:{{ .Values.gateway.port }}
{{- end -}}
{{- end -}}
