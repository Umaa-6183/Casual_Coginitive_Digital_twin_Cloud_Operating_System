variable "name_prefix" {
  description = "Prefix applied to all resource names (e.g. ccdt-prod)"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]{3,24}$", var.name_prefix))
    error_message = "name_prefix must be 3-24 lowercase alphanumeric characters or hyphens."
  }
}

variable "cluster_name" {
  description = "EKS cluster name — used for Kubernetes subnet tags"
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC (recommend /16 for large clusters)"
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid CIDR block."
  }
}

variable "az_count" {
  description = "Number of Availability Zones to use (2 or 3 recommended)"
  type        = number
  default     = 3

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "aws_region" {
  description = "AWS region for VPC endpoint service names"
  type        = string
  default     = "us-east-1"
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}
