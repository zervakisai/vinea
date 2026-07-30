# The PAID path (deferred, not rejected -- ADR-006). Nothing here runs in the
# free deployment; the kind e2e needs no cloud at all. This module exists so that
# "move to a permanent URL" is a decision, not a project.
#
# OpenTofu rather than Terraform: MPL-licensed, CLI-compatible, and consistent
# with this phase's binding constraint (free, no vendor strings attached).
#
# CI runs `tofu fmt -check` and `tofu validate` (init -backend=false) -- and
# STOPS there. `plan` queries provider APIs, so it needs real credentials and a
# billing account; pretending a credential-less plan verifies anything would be
# exactly the unexercised-claim problem this file exists to remove. Plan/apply are
# documented below as the manual step they are.
#
#   tofu init && tofu plan -var project_id=... && tofu apply
#
# State: this module configures no remote backend on purpose. State lands where
# you tell `tofu init` to put it, and that bootstrap (a bucket that must exist
# before the tool that creates buckets runs) is the operator's one manual step.

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# --- cluster: zonal, because the control plane of one zonal cluster is free ----
# (a regional cluster bills from minute one). One small autoscaling pool; the
# workload is two small web processes and a nightly batch.
resource "google_container_cluster" "vinea" {
  name     = "vinea"
  location = var.zone

  # We manage the pool explicitly below; the default pool is deleted at create.
  remove_default_node_pool = true
  initial_node_count       = 1

  deletion_protection = false
}

resource "google_container_node_pool" "default" {
  name     = "default"
  cluster  = google_container_cluster.vinea.id
  location = var.zone

  autoscaling {
    min_node_count = 1
    max_node_count = 2
  }

  node_config {
    machine_type = var.machine_type
    disk_size_gb = 30
    # Spot nodes: the API tolerates a restart (stateless, ADR-003's queue
    # survives the worker), and the price difference is the point of this file.
    spot = true
  }
}

# --- database: managed, OUTSIDE the cluster (ADR-001 / ADR-006) ----------------
# The advisories are the one thing that cannot be recomputed; they do not live
# on the newest component in the system.
resource "google_sql_database_instance" "postgres" {
  name             = "vinea-postgres"
  database_version = "POSTGRES_16"
  # EU residency is an ADR-001/ADR-004 constraint, enforced here by the variable
  # validation in variables.tf rather than by trusting the operator's memory.
  region = var.region

  settings {
    tier    = var.db_tier
    edition = "ENTERPRISE"

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      # Private services access is the production answer; it needs a VPC peering
      # dance that triples this file. Public IP + authorized networks is honest
      # for the first paid deployment, and the flag below keeps TLS mandatory.
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }
  }

  deletion_protection = true
}

resource "google_sql_database" "vinea" {
  name     = "vinea"
  instance = google_sql_database_instance.postgres.name
}

resource "google_sql_user" "vinea" {
  name     = "vinea"
  instance = google_sql_database_instance.postgres.name
  password = var.db_password # sensitive; supplied at plan time, never committed
}

# --- registry: where CI pushes the two images ---------------------------------
resource "google_artifact_registry_repository" "images" {
  repository_id = "vinea"
  format        = "DOCKER"
  location      = var.region
}
