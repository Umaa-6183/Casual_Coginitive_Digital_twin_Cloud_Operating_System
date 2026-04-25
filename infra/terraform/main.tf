# ═══════════════════════════════════════════════════════════════════════════════
# CCDT — Terraform Root Configuration
# ═══════════════════════════════════════════════════════════════════════════════
# Orchestrates all CCDT AWS infrastructure:
#   1. VPC (subnets, NAT GWs, endpoints, flow logs)
#   2. EKS (cluster, 3 node groups, IRSA roles, add-ons)
#   3. RDS (Aurora PostgreSQL Serverless v2 for incident store)
#
# Remote state: S3 + DynamoDB locking
# Secrets:      AWS Secrets Manager (no plaintext secrets in state)
#
# Quick start:
#   # 1. Create S3 bucket + DynamoDB table for state (one-time setup)
#   aws s3 mb s3://ccdt-terraform-state-${AWS_ACCOUNT_ID} --region us-east-1
#   aws s3api put-bucket-versioning \
#     --bucket ccdt-terraform-state-${AWS_ACCOUNT_ID} \
#     --versioning-configuration Status=Enabled
#   aws dynamodb create-table \
#     --table-name ccdt-terraform-locks \
#     --attribute-definitions AttributeName=LockID,AttributeType=S \
#     --key-schema AttributeName=LockID,KeyType=HASH \
#     --billing-mode PAY_PER_REQUEST \
#     --region us-east-1
#
#   # 2. Update backend config below with your account ID
#   # 3. Deploy
#   terraform init
#   terraform plan -var-file=terraform.tfvars
#   terraform apply -var-file=terraform.tfvars
#
#   # 4. Configure kubectl
#   aws eks update-kubeconfig --region us-east-1 --name ccdt-prod
#
#   # 5. Deploy CCDT via Helm
#   helm upgrade --install ccdt ./infra/helm/ccdt \
#     --namespace ccdt --create-namespace \
#     -f infra/helm/ccdt/values.yaml
# ═══════════════════════════════════════════════════════════════════════════════

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # ── Remote State Backend ──────────────────────────────────────────────────
  # IMPORTANT: Replace <YOUR_AWS_ACCOUNT_ID> before running terraform init.
  # Use partial configuration and pass backend config via -backend-config flag:
  #   terraform init -backend-config=backend.hcl
  # Or inline (below) — update bucket name to match your account.
  backend "s3" {
    bucket         = "ccdt-terraform-state"   # ← update with your bucket name
    key            = "ccdt/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "ccdt-terraform-locks"
    # Optional: KMS key for state encryption
    # kms_key_id = "alias/terraform-state"
  }
}

# ── Provider ──────────────────────────────────────────────────────────────────
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

# ── Locals ────────────────────────────────────────────────────────────────────
locals {
  name_prefix = "${var.project}-${var.environment}"

  common_tags = merge({
    Project     = var.project
    Environment = var.environment
    Owner       = var.owner
    ManagedBy   = "terraform"
    Repository  = "ccdt"
  }, var.additional_tags)
}

data "aws_caller_identity" "current" {}
data "aws_region"          "current" {}

# ═══════════════════════════════════════════════════════════════════════════════
# Module 1: VPC
# ═══════════════════════════════════════════════════════════════════════════════
module "vpc" {
  source = "./modules/vpc"

  name_prefix  = local.name_prefix
  cluster_name = var.cluster_name
  vpc_cidr     = var.vpc_cidr
  az_count     = var.az_count
  aws_region   = var.aws_region
  tags         = local.common_tags
}

# ═══════════════════════════════════════════════════════════════════════════════
# Module 2: EKS
# ═══════════════════════════════════════════════════════════════════════════════
module "eks" {
  source = "./modules/eks"

  cluster_name        = var.cluster_name
  kubernetes_version  = var.kubernetes_version
  vpc_id              = module.vpc.vpc_id
  vpc_cidr            = module.vpc.vpc_cidr
  private_subnet_ids  = module.vpc.private_subnet_ids
  public_subnet_ids   = module.vpc.public_subnet_ids
  public_api_endpoint = var.public_api_endpoint
  public_api_cidrs    = var.public_api_cidrs
  admin_iam_roles     = var.admin_iam_roles

  # Node groups
  system_node_instance_type  = var.system_node_instance_type
  system_node_desired        = var.system_node_desired
  system_node_min            = var.system_node_desired  # never scale below desired for system
  system_node_max            = var.system_node_desired + 3

  compute_node_instance_type = var.compute_node_instance_type
  compute_node_desired       = var.compute_node_desired
  compute_node_min           = var.compute_node_min
  compute_node_max           = var.compute_node_max

  ebpf_node_instance_type    = var.ebpf_node_instance_type
  ebpf_node_count            = var.ebpf_node_count

  tags = local.common_tags

  depends_on = [module.vpc]
}

# ═══════════════════════════════════════════════════════════════════════════════
# Module 3: RDS (Aurora PostgreSQL)
# ═══════════════════════════════════════════════════════════════════════════════
module "rds" {
  source = "./modules/rds"

  name_prefix          = local.name_prefix
  vpc_id               = module.vpc.vpc_id
  db_subnet_group_name = module.vpc.db_subnet_group_name

  # Allow EKS nodes to connect to RDS
  allowed_security_group_ids = [module.eks.node_security_group_id]
  allowed_cidr_blocks        = module.vpc.private_subnet_ids == [] ? [] : [module.vpc.vpc_cidr]

  db_name            = var.db_name
  db_master_username = var.db_master_username

  serverless_min_capacity = var.rds_min_capacity
  serverless_max_capacity = var.rds_max_capacity
  reader_count            = var.rds_reader_count

  backup_retention_days = var.rds_backup_retention_days
  deletion_protection   = var.rds_deletion_protection
  skip_final_snapshot   = var.rds_skip_final_snapshot

  secrets_rotation_lambda_arn = var.secrets_rotation_lambda_arn
  cloudwatch_alarm_sns_arn    = var.alarm_sns_arn

  tags = local.common_tags

  depends_on = [module.vpc]
}

# ═══════════════════════════════════════════════════════════════════════════════
# ECR Repositories (one per CCDT layer)
# ═══════════════════════════════════════════════════════════════════════════════
locals {
  ecr_repos = [
    "layer1-nervous",
    "layer2-cognitive",
    "layer3-guardian",
    "layer4-copilot",
    "api-gateway",
    "dashboard",
  ]
}

resource "aws_ecr_repository" "ccdt" {
  for_each = toset(local.ecr_repos)

  name                 = "${local.name_prefix}/${each.value}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }

  tags = merge(local.common_tags, {
    Component = each.value
  })
}

resource "aws_ecr_lifecycle_policy" "ccdt" {
  for_each   = aws_ecr_repository.ccdt
  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 tagged release images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Remove untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      }
    ]
  })
}

# ═══════════════════════════════════════════════════════════════════════════════
# SSM Parameter Store: Non-sensitive config for CCDT pods
# ═══════════════════════════════════════════════════════════════════════════════
resource "aws_ssm_parameter" "kafka_bootstrap" {
  name        = "/${local.name_prefix}/kafka/bootstrap-servers"
  type        = "String"
  value       = "kafka.${var.cluster_name}.svc.cluster.local:9092"
  description = "Kafka bootstrap server address for CCDT services"
  tags        = local.common_tags
}

resource "aws_ssm_parameter" "rds_endpoint" {
  name        = "/${local.name_prefix}/rds/endpoint"
  type        = "String"
  value       = module.rds.cluster_endpoint
  description = "Aurora PostgreSQL writer endpoint"
  tags        = local.common_tags
}

resource "aws_ssm_parameter" "eks_cluster_name" {
  name        = "/${local.name_prefix}/eks/cluster-name"
  type        = "String"
  value       = module.eks.cluster_name
  description = "EKS cluster name"
  tags        = local.common_tags
}

# ═══════════════════════════════════════════════════════════════════════════════
# Outputs
# ═══════════════════════════════════════════════════════════════════════════════

# ── VPC Outputs ────────────────────────────────────────────────────────────────
output "vpc_id" {
  description = "VPC ID"
  value       = module.vpc.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnet IDs (EKS nodes)"
  value       = module.vpc.private_subnet_ids
}

output "nat_public_ips" {
  description = "NAT Gateway public IPs — whitelist in external services"
  value       = module.vpc.nat_public_ips
}

# ── EKS Outputs ────────────────────────────────────────────────────────────────
output "cluster_name" {
  description = "EKS cluster name"
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS API server endpoint"
  value       = module.eks.cluster_endpoint
  sensitive   = true
}

output "kubeconfig_command" {
  description = "Run this command to configure kubectl"
  value       = module.eks.kubeconfig_command
}

output "cluster_autoscaler_role_arn" {
  description = "IRSA role ARN for Cluster Autoscaler"
  value       = module.eks.cluster_autoscaler_role_arn
}

output "alb_controller_role_arn" {
  description = "IRSA role ARN for AWS Load Balancer Controller"
  value       = module.eks.alb_controller_role_arn
}

output "oidc_provider_arn" {
  description = "OIDC provider ARN (for additional IRSA roles)"
  value       = module.eks.oidc_provider_arn
}

# ── RDS Outputs ────────────────────────────────────────────────────────────────
output "rds_endpoint" {
  description = "Aurora PostgreSQL writer endpoint"
  value       = module.rds.cluster_endpoint
  sensitive   = true
}

output "rds_reader_endpoint" {
  description = "Aurora PostgreSQL reader endpoint"
  value       = module.rds.reader_endpoint
}

output "rds_secret_arn" {
  description = "Secrets Manager ARN for RDS credentials"
  value       = module.rds.secret_arn
}

# ── ECR Outputs ────────────────────────────────────────────────────────────────
output "ecr_repository_urls" {
  description = "ECR repository URLs for all CCDT components"
  value       = { for k, v in aws_ecr_repository.ccdt : k => v.repository_url }
}

# ── Helm values override for CI/CD ────────────────────────────────────────────
output "helm_values_override" {
  description = "Paste these into your CI/CD pipeline as Helm --set arguments"
  value = <<-EOT
    global.imageRegistry=${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${local.name_prefix}
    global.rdsEndpoint=${module.rds.cluster_endpoint}
    global.eksClusterName=${module.eks.cluster_name}
    global.region=${var.aws_region}
  EOT
  sensitive = true
}
