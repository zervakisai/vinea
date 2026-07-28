output "cluster_name" {
  description = "Feed to: gcloud container clusters get-credentials <name> --zone <zone>"
  value       = google_container_cluster.vinea.name
}

output "cluster_endpoint" {
  description = "The API server address."
  value       = google_container_cluster.vinea.endpoint
  # Not secret, but not interesting either, and printing it on every apply
  # invites pasting it into places it does not belong.
  sensitive = true
}

output "registry" {
  description = "Where CI pushes the two images. Feeds values.yaml's image.repository."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "database_connection_name" {
  description = "Cloud SQL instance connection name, for the Cloud SQL Auth Proxy sidecar."
  value       = google_sql_database_instance.postgres.connection_name
}

# Deliberately NOT an output: the assembled DATABASE_URL.
#
# It would be the convenient thing to emit, and it would put a live password into
# tofu's state file and into the terminal scrollback of everyone who runs
# `tofu output`. The chart reads DATABASE_URL from a Secret it only ever
# references by name (infra/sealed-secrets/README.md); assembling it here would
# route a credential through a second place for the sake of one less copy-paste.
