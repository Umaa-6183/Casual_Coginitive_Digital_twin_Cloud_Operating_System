output "cluster_id" {
  description = "Aurora cluster ID"
  value       = aws_rds_cluster.main.id
}

output "cluster_arn" {
  description = "Aurora cluster ARN"
  value       = aws_rds_cluster.main.arn
}

output "cluster_endpoint" {
  description = "Aurora writer endpoint (use for reads/writes)"
  value       = aws_rds_cluster.main.endpoint
}

output "reader_endpoint" {
  description = "Aurora reader endpoint (load-balanced across all readers)"
  value       = aws_rds_cluster.main.reader_endpoint
}

output "port" {
  description = "Database port"
  value       = aws_rds_cluster.main.port
}

output "database_name" {
  description = "Name of the initial database"
  value       = aws_rds_cluster.main.database_name
}

output "master_username" {
  description = "Master username"
  value       = aws_rds_cluster.main.master_username
  sensitive   = true
}

output "secret_arn" {
  description = "ARN of the Secrets Manager secret containing database credentials"
  value       = aws_secretsmanager_secret.rds.arn
}

output "secret_name" {
  description = "Name of the Secrets Manager secret"
  value       = aws_secretsmanager_secret.rds.name
}

output "security_group_id" {
  description = "Security group ID of the RDS cluster"
  value       = aws_security_group.rds.id
}

output "kms_key_arn" {
  description = "KMS key ARN used for RDS encryption"
  value       = aws_kms_key.rds.arn
}

output "writer_instance_id" {
  description = "Instance ID of the Aurora writer"
  value       = aws_rds_cluster_instance.writer.identifier
}

output "reader_instance_ids" {
  description = "Instance IDs of Aurora reader(s)"
  value       = aws_rds_cluster_instance.reader[*].identifier
}

output "connection_string_ssm_path" {
  description = "Path hint: store DSN in SSM Parameter Store at this path"
  value       = "/${var.name_prefix}/rds/connection-string"
}
