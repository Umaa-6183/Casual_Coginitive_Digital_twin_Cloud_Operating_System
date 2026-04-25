# ═══════════════════════════════════════════════════════════════════════════════
# CCDT — Terraform Root Variables
# ═══════════════════════════════════════════════════════════════════════════════
# Override defaults in a terraform.tfvars or via -var flags.
# Example terraform.tfvars:
#
#   aws_region    = "us-east-1"
#   environment   = "prod"
#   cluster_name  = "ccdt-prod"
#   vpc_cidr      = "10.0.0.0/16"
#   admin_iam_roles = ["arn:aws:iam::123456789012:role/DevOpsAdmins"]
# ═══════════════════════════════════════════════════════════════════════════════

# ── Global ────────────────────────────────────────────────────────────────────
variable "aws_region" {
  description = "AWS region to deploy CCDT infrastructure"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment (dev | staging | prod)"
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "project" {
  description = "Project name — used as tag and resource prefix"
  type        = string
  default     = "ccdt"
}

variable "owner" {
  description = "Team owner tag for all resources"
  type        = string
  default     = "platform-engineering"
}

# ── VPC ───────────────────────────────────────────────────────────────────────
variable "vpc_cidr" {
  description = "CIDR block for the main VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Number of Availability Zones to use (2 or 3)"
  type        = number
  default     = 3
}

# ── EKS ───────────────────────────────────────────────────────────────────────
variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
  default     = "ccdt-prod"
}

variable "kubernetes_version" {
  description = "Kubernetes version for the EKS cluster"
  type        = string
  default     = "1.30"
}

variable "public_api_endpoint" {
  description = "Whether to expose the EKS API endpoint publicly (false for strict prod)"
  type        = bool
  default     = false
}

variable "public_api_cidrs" {
  description = "CIDRs allowed to access the EKS public API (if enabled). Restrict to VPN CIDRs."
  type        = list(string)
  default     = []
}

variable "admin_iam_roles" {
  description = "IAM role ARNs to grant EKS cluster-admin access"
  type        = list(string)
  default     = []
}

# ── Node Groups ───────────────────────────────────────────────────────────────
variable "system_node_instance_type" {
  description = "EC2 instance type for system node group"
  type        = string
  default     = "t3.large"
}

variable "system_node_desired" {
  description = "Desired nodes in system node group"
  type        = number
  default     = 3
}

variable "compute_node_instance_type" {
  description = "EC2 instance type for compute node group (CCDT layers 2/3/4)"
  type        = string
  default     = "m5.xlarge"
}

variable "compute_node_desired" {
  description = "Desired nodes in compute node group"
  type        = number
  default     = 3
}

variable "compute_node_min" {
  description = "Minimum compute nodes (cluster autoscaler floor)"
  type        = number
  default     = 2
}

variable "compute_node_max" {
  description = "Maximum compute nodes (cluster autoscaler ceiling)"
  type        = number
  default     = 10
}

variable "ebpf_node_instance_type" {
  description = "EC2 instance type for eBPF DaemonSet nodes"
  type        = string
  default     = "m5.large"
}

variable "ebpf_node_count" {
  description = "Number of eBPF nodes"
  type        = number
  default     = 3
}

# ── RDS ───────────────────────────────────────────────────────────────────────
variable "db_name" {
  description = "Initial database name for CCDT incident store"
  type        = string
  default     = "ccdt_incidents"
}

variable "db_master_username" {
  description = "RDS master username"
  type        = string
  default     = "ccdt_admin"
  sensitive   = true
}

variable "rds_min_capacity" {
  description = "Aurora Serverless v2 minimum capacity (ACUs)"
  type        = number
  default     = 0.5
}

variable "rds_max_capacity" {
  description = "Aurora Serverless v2 maximum capacity (ACUs)"
  type        = number
  default     = 16
}

variable "rds_reader_count" {
  description = "Number of Aurora reader instances"
  type        = number
  default     = 1
}

variable "rds_backup_retention_days" {
  description = "RDS automated backup retention (days)"
  type        = number
  default     = 14
}

variable "rds_deletion_protection" {
  description = "Enable RDS deletion protection (always true in prod)"
  type        = bool
  default     = true
}

variable "rds_skip_final_snapshot" {
  description = "Skip final RDS snapshot on deletion (false in prod)"
  type        = bool
  default     = false
}

# ── Notifications ─────────────────────────────────────────────────────────────
variable "alarm_sns_arn" {
  description = "SNS topic ARN for CloudWatch alarm notifications (leave empty to skip)"
  type        = string
  default     = ""
}

variable "secrets_rotation_lambda_arn" {
  description = "ARN of Secrets Manager rotation Lambda for RDS passwords"
  type        = string
  default     = ""
}

# ── Tags ──────────────────────────────────────────────────────────────────────
variable "additional_tags" {
  description = "Additional tags to merge onto all resources"
  type        = map(string)
  default     = {}
}
