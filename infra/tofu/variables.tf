variable "project_id" {
  description = "GCP project that owns the cluster, database and registry."
  type        = string
}

variable "region" {
  description = "Region for Cloud SQL and Artifact Registry. Must be in the EU."
  type        = string
  default     = "europe-west1"

  # ADR-001 and ADR-004 keep grower data on EU-resident infrastructure, and
  # `grower_config.region` records which region a tenant is bound to. That is a
  # promise made in prose in two ADRs; here it becomes a rule the tool enforces.
  # A `plan` against us-central1 fails, rather than succeeding quietly and moving
  # a grower's advisories across a border that a comment was supposed to guard.
  validation {
    condition     = startswith(var.region, "europe-")
    error_message = "EU data residency (ADR-001/ADR-004): region must start with 'europe-'."
  }
}

variable "zone" {
  description = "Zone for the GKE cluster. Zonal, not regional: one zonal control plane per billing account is free, a regional one bills from the first minute."
  type        = string
  default     = "europe-west1-b"

  validation {
    condition     = startswith(var.zone, "europe-")
    error_message = "EU data residency (ADR-001/ADR-004): zone must start with 'europe-'."
  }
}

variable "machine_type" {
  description = "Node machine type. The workload is two small web processes and a nightly batch; this is sized for that, not for headroom nobody uses."
  type        = string
  default     = "e2-small"
}

variable "db_tier" {
  description = "Cloud SQL tier. db-f1-micro is the cheapest that exists and is enough for one vineyard's advisories and the FAO-56 corpus, which is retrieved with full-text search rather than vectors (ADR-011)."
  type        = string
  default     = "db-f1-micro"
}

variable "db_password" {
  description = "Password for the 'vinea' database role. Supplied at plan time (TF_VAR_db_password or -var-file), never committed — the house rule is that secrets do not live in tracked files, and a .tfvars file is a tracked file unless you are careful."
  type        = string
  sensitive   = true
}
