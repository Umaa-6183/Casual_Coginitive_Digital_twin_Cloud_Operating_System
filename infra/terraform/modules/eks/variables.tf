variable "cluster_name" {
  description = "EKS cluster name"
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{1,99}$", var.cluster_name))
    error_message = "Cluster name must start with a letter and be 2-100 characters."
  }
}

variable "kubernetes_version" {
  description = "Kubernetes version for the EKS cluster"
  type        = string
  default     = "1.30"
}

variable "vpc_id" {
  description = "VPC ID to deploy the EKS cluster into"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR block (used for security group rules)"
  type        = string
}

variable "private_subnet_ids" {
  description = "List of private subnet IDs for EKS node groups"
  type        = list(string)
}

variable "public_subnet_ids" {
  description = "List of public subnet IDs (for control plane cross-AZ access)"
  type        = list(string)
}

variable "service_cidr" {
  description = "CIDR block for Kubernetes Services (must not overlap VPC or pod CIDR)"
  type        = string
  default     = "172.20.0.0/16"
}

variable "public_api_endpoint" {
  description = "Whether to enable public access to the EKS API endpoint"
  type        = bool
  default     = false  # private-only for production
}

variable "public_api_cidrs" {
  description = "CIDRs allowed to access the public EKS API endpoint (if enabled)"
  type        = list(string)
  default     = ["0.0.0.0/0"]  # restrict to your office/VPN IPs in prod
}

# ── Node Group: System ────────────────────────────────────────────────────────
variable "system_node_instance_type" {
  description = "EC2 instance type for system node group"
  type        = string
  default     = "t3.large"
}

variable "system_node_desired" {
  description = "Desired number of system nodes"
  type        = number
  default     = 3
}

variable "system_node_min" {
  description = "Minimum number of system nodes"
  type        = number
  default     = 3
}

variable "system_node_max" {
  description = "Maximum number of system nodes"
  type        = number
  default     = 6
}

# ── Node Group: Compute ───────────────────────────────────────────────────────
variable "compute_node_instance_type" {
  description = "EC2 instance type for compute node group (CCDT layers 2/3/4)"
  type        = string
  default     = "m5.xlarge"
}

variable "compute_node_desired" {
  description = "Desired number of compute nodes"
  type        = number
  default     = 3
}

variable "compute_node_min" {
  description = "Minimum number of compute nodes"
  type        = number
  default     = 2
}

variable "compute_node_max" {
  description = "Maximum number of compute nodes (cluster autoscaler ceiling)"
  type        = number
  default     = 10
}

# ── Node Group: eBPF ─────────────────────────────────────────────────────────
variable "ebpf_node_instance_type" {
  description = "EC2 instance type for eBPF DaemonSet nodes"
  type        = string
  default     = "m5.large"
}

variable "ebpf_node_count" {
  description = "Number of eBPF nodes (typically 1 per worker node)"
  type        = number
  default     = 3
}

# ── EKS Add-on versions ───────────────────────────────────────────────────────
variable "coredns_version" {
  description = "CoreDNS add-on version"
  type        = string
  default     = "v1.11.1-eksbuild.9"
}

variable "kube_proxy_version" {
  description = "kube-proxy add-on version"
  type        = string
  default     = "v1.30.0-eksbuild.3"
}

variable "vpc_cni_version" {
  description = "VPC CNI add-on version"
  type        = string
  default     = "v1.18.3-eksbuild.1"
}

variable "ebs_csi_version" {
  description = "EBS CSI driver add-on version"
  type        = string
  default     = "v1.31.0-eksbuild.1"
}

# ── Access Control ────────────────────────────────────────────────────────────
variable "admin_iam_roles" {
  description = "List of IAM role ARNs to grant cluster-admin access"
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}
