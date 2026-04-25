variable "name_prefix" {
  description = "Prefix for all RDS resource names"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID to deploy RDS into"
  type        = string
}

variable "db_subnet_group_name" {
  description = "Name of the DB subnet group (from VPC module output)"
  type        = string
}

variable "allowed_security_group_ids" {
  description = "Security group IDs allowed to connect to RDS (EKS node SGs)"
  type        = list(string)
  default     = []
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks allowed to connect to RDS (private subnets)"
  type        = list(string)
  default     = []
}

variable "db_name" {
  description = "Name of the initial database to create"
  type        = string
  default     = "ccdt_incidents"
}

variable "db_master_username" {
  description = "Master username for the RDS cluster"
  type        = string
  default     = "ccdt_admin"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,15}$", var.db_master_username))
    error_message = "db_master_username must start with a letter, 1-16 alphanumeric/underscore chars."
  }
}

variable "engine_version" {
  description = "Aurora PostgreSQL engine version"
  type        = string
  default     = "16.2"
}

variable "serverless_min_capacity" {
  description = "Aurora Serverless v2 minimum capacity in ACUs (0.5 = half vCPU)"
  type        = number
  default     = 0.5

  validation {
    condition     = var.serverless_min_capacity >= 0.5 && var.serverless_min_capacity <= 128
    error_message = "Minimum capacity must be between 0.5 and 128 ACUs."
  }
}

variable "serverless_max_capacity" {
  description = "Aurora Serverless v2 maximum capacity in ACUs"
  type        = number
  default     = 16

  validation {
    condition     = var.serverless_max_capacity >= 1 && var.serverless_max_capacity <= 128
    error_message = "Maximum capacity must be between 1 and 128 ACUs."
  }
}

variable "reader_count" {
  description = "Number of Aurora reader instances (0 for single writer only)"
  type        = number
  default     = 1
}

variable "backup_retention_days" {
  description = "Number of days to retain automated backups (1-35)"
  type        = number
  default     = 14

  validation {
    condition     = var.backup_retention_days >= 1 && var.backup_retention_days <= 35
    error_message = "Backup retention must be between 1 and 35 days."
  }
}

variable "deletion_protection" {
  description = "Enable deletion protection on the RDS cluster"
  type        = bool
  default     = true
}

variable "skip_final_snapshot" {
  description = "Skip final snapshot on cluster deletion (set false in production)"
  type        = bool
  default     = false
}

variable "secrets_rotation_lambda_arn" {
  description = "ARN of the Secrets Manager rotation Lambda (leave empty to skip rotation)"
  type        = string
  default     = ""
}

variable "cloudwatch_alarm_sns_arn" {
  description = "SNS topic ARN for CloudWatch alarm notifications (leave empty to skip)"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}
