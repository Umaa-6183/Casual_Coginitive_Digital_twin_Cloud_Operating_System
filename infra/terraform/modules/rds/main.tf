# ═══════════════════════════════════════════════════════════════════════════════
# CCDT — Terraform Module: Amazon RDS (Aurora PostgreSQL)
# ═══════════════════════════════════════════════════════════════════════════════
# Creates an Aurora PostgreSQL Serverless v2 cluster for CCDT:
#   • Aurora PostgreSQL 16.x Serverless v2 (auto-scales 0.5–16 ACUs)
#   • Multi-AZ: 1 writer + 1 reader replica
#   • Storage: gp3, encrypted with customer-managed KMS key
#   • Enhanced Monitoring (60s interval)
#   • Performance Insights (7-day retention)
#   • Automated backups: 14-day retention
#   • Point-in-time recovery enabled
#   • Database: ccdt_incidents (CCDT incident history + Guardian action log)
#   • Secret rotation: AWS Secrets Manager (30-day auto-rotation)
# ═══════════════════════════════════════════════════════════════════════════════

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

data "aws_region"  "current" {}
data "aws_partition" "current" {}

locals {
  common_tags = merge(var.tags, {
    ManagedBy = "terraform"
    Module    = "rds"
  })
}

# ── Master password (stored in Secrets Manager, rotated every 30 days) ────────
resource "random_password" "master" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_secretsmanager_secret" "rds" {
  name                    = "${var.name_prefix}/rds/master-credentials"
  description             = "CCDT RDS Aurora PostgreSQL master credentials"
  kms_key_id              = aws_kms_key.rds.arn
  recovery_window_in_days = 7

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-rds-secret"
  })
}

resource "aws_secretsmanager_secret_version" "rds" {
  secret_id = aws_secretsmanager_secret.rds.id
  secret_string = jsonencode({
    username = var.db_master_username
    password = random_password.master.result
    host     = aws_rds_cluster.main.endpoint
    port     = 5432
    dbname   = var.db_name
    engine   = "aurora-postgresql"
    url      = "postgresql://${var.db_master_username}:${random_password.master.result}@${aws_rds_cluster.main.endpoint}:5432/${var.db_name}"
  })

  lifecycle {
    ignore_changes = [secret_string]  # let rotation update the password
  }
}

# ── KMS Key: RDS Encryption ───────────────────────────────────────────────────
resource "aws_kms_key" "rds" {
  description             = "KMS key for CCDT RDS Aurora encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableRootAccess"
        Effect = "Allow"
        Principal = {
          AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowRDS"
        Effect = "Allow"
        Principal = { Service = "rds.amazonaws.com" }
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource = "*"
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-rds-kms"
  })
}

resource "aws_kms_alias" "rds" {
  name          = "alias/${var.name_prefix}-rds"
  target_key_id = aws_kms_key.rds.key_id
}

data "aws_caller_identity" "current" {}

# ── Security Group: RDS ───────────────────────────────────────────────────────
resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds-sg"
  description = "CCDT Aurora PostgreSQL — allow access from EKS nodes and app subnets only"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = var.allowed_security_group_ids
    description     = "PostgreSQL from EKS nodes"
  }

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidr_blocks
    description = "PostgreSQL from allowed CIDRs (private subnets)"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-rds-sg"
  })
}

# ── RDS Parameter Group: PostgreSQL 16 tuning ────────────────────────────────
resource "aws_rds_cluster_parameter_group" "main" {
  name        = "${var.name_prefix}-aurora-pg16"
  family      = "aurora-postgresql16"
  description = "CCDT Aurora PostgreSQL 16 cluster parameter group"

  # Connection pooling — let application handle via PgBouncer
  parameter {
    name  = "max_connections"
    value = "200"
  }

  # WAL settings for reliability
  parameter {
    name  = "wal_level"
    value = "logical"
  }

  # Logging — capture slow queries (> 100ms)
  parameter {
    name  = "log_min_duration_statement"
    value = "100"
    apply_method = "immediate"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }

  parameter {
    name  = "log_lock_waits"
    value = "1"
  }

  # Shared buffers — 25% of instance RAM (managed by Aurora Serverless)
  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements,auto_explain"
  }

  parameter {
    name  = "pg_stat_statements.track"
    value = "ALL"
  }

  parameter {
    name  = "auto_explain.log_min_duration"
    value = "1000"  # 1 second
  }

  tags = local.common_tags
}

resource "aws_db_parameter_group" "main" {
  name        = "${var.name_prefix}-aurora-pg16-instance"
  family      = "aurora-postgresql16"
  description = "CCDT Aurora PostgreSQL 16 instance parameter group"

  tags = local.common_tags
}

# ── Aurora Cluster ────────────────────────────────────────────────────────────
resource "aws_rds_cluster" "main" {
  cluster_identifier = "${var.name_prefix}-aurora-pg"

  engine         = "aurora-postgresql"
  engine_version = var.engine_version
  engine_mode    = "provisioned"  # Serverless v2 uses provisioned mode

  database_name   = var.db_name
  master_username = var.db_master_username
  master_password = random_password.master.result

  db_subnet_group_name            = var.db_subnet_group_name
  vpc_security_group_ids          = [aws_security_group.rds.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.main.name

  # Encryption
  storage_encrypted = true
  kms_key_id        = aws_kms_key.rds.arn

  # Backup
  backup_retention_period      = var.backup_retention_days
  preferred_backup_window      = "03:00-04:00"   # UTC
  preferred_maintenance_window = "mon:04:00-mon:05:00"

  # Deletion protection — ALWAYS true in prod
  deletion_protection = var.deletion_protection

  # Skip final snapshot in non-prod; always true in prod
  skip_final_snapshot       = var.skip_final_snapshot
  final_snapshot_identifier = var.skip_final_snapshot ? null : "${var.name_prefix}-final-${formatdate("YYYYMMDD", timestamp())}"

  # Enable PITR
  copy_tags_to_snapshot = true

  # CloudWatch logs
  enabled_cloudwatch_logs_exports = ["postgresql"]

  # Serverless v2 scaling configuration
  serverlessv2_scaling_configuration {
    min_capacity = var.serverless_min_capacity
    max_capacity = var.serverless_max_capacity
  }

  apply_immediately = false  # only in maintenance window for prod

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-aurora-cluster"
  })

  lifecycle {
    ignore_changes = [master_password]  # managed by Secrets Manager rotation
  }
}

# ── Aurora Cluster Instance: Writer ──────────────────────────────────────────
resource "aws_rds_cluster_instance" "writer" {
  identifier         = "${var.name_prefix}-aurora-writer"
  cluster_identifier = aws_rds_cluster.main.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.main.engine
  engine_version     = aws_rds_cluster.main.engine_version

  db_parameter_group_name = aws_db_parameter_group.main.name

  # Enhanced Monitoring
  monitoring_role_arn = aws_iam_role.rds_monitoring.arn
  monitoring_interval = 60

  # Performance Insights
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.rds.arn
  performance_insights_retention_period = 7

  auto_minor_version_upgrade = true
  publicly_accessible        = false

  apply_immediately = false

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-aurora-writer"
    Role = "writer"
  })
}

# ── Aurora Cluster Instance: Reader ──────────────────────────────────────────
resource "aws_rds_cluster_instance" "reader" {
  count = var.reader_count

  identifier         = "${var.name_prefix}-aurora-reader-${count.index + 1}"
  cluster_identifier = aws_rds_cluster.main.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.main.engine
  engine_version     = aws_rds_cluster.main.engine_version

  db_parameter_group_name = aws_db_parameter_group.main.name

  monitoring_role_arn = aws_iam_role.rds_monitoring.arn
  monitoring_interval = 60

  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.rds.arn
  performance_insights_retention_period = 7

  auto_minor_version_upgrade = true
  publicly_accessible        = false

  apply_immediately = false

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-aurora-reader-${count.index + 1}"
    Role = "reader"
  })
}

# ── IAM Role: Enhanced Monitoring ────────────────────────────────────────────
resource "aws_iam_role" "rds_monitoring" {
  name = "${var.name_prefix}-rds-monitoring-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

# ── Secrets Manager: Auto-Rotation ───────────────────────────────────────────
resource "aws_secretsmanager_secret_rotation" "rds" {
  secret_id           = aws_secretsmanager_secret.rds.id
  rotation_lambda_arn = var.secrets_rotation_lambda_arn

  rotation_rules {
    automatically_after_days = 30
  }

  # Only enable if a rotation Lambda ARN is provided
  count = var.secrets_rotation_lambda_arn != "" ? 1 : 0
}

# ── CloudWatch Alarms ─────────────────────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${var.name_prefix}-rds-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "Aurora writer CPU > 80% for 10 minutes"
  treat_missing_data  = "notBreaching"

  dimensions = {
    DBClusterIdentifier = aws_rds_cluster.main.cluster_identifier
  }

  alarm_actions = var.cloudwatch_alarm_sns_arn != "" ? [var.cloudwatch_alarm_sns_arn] : []
  ok_actions    = var.cloudwatch_alarm_sns_arn != "" ? [var.cloudwatch_alarm_sns_arn] : []

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "rds_connections" {
  alarm_name          = "${var.name_prefix}-rds-connections-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "DatabaseConnections"
  namespace           = "AWS/RDS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 180  # 90% of max_connections=200
  alarm_description   = "Aurora connections > 180 (90% of limit)"

  dimensions = {
    DBClusterIdentifier = aws_rds_cluster.main.cluster_identifier
  }

  alarm_actions = var.cloudwatch_alarm_sns_arn != "" ? [var.cloudwatch_alarm_sns_arn] : []

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "rds_freeable_memory" {
  alarm_name          = "${var.name_prefix}-rds-memory-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "FreeableMemory"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 268435456  # 256 MB
  alarm_description   = "Aurora freeable memory < 256 MB"

  dimensions = {
    DBInstanceIdentifier = aws_rds_cluster_instance.writer.identifier
  }

  alarm_actions = var.cloudwatch_alarm_sns_arn != "" ? [var.cloudwatch_alarm_sns_arn] : []

  tags = local.common_tags
}
