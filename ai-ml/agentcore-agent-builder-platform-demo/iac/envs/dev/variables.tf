variable "aws_region" {
  type        = string
  default     = "ap-northeast-2"
  description = "AWS region for deployment"
}

variable "project" {
  type        = string
  default     = "aiops-v2"
  description = "Project name prefix"
}

variable "env" {
  type        = string
  default     = "dev"
  description = "Environment (dev/staging/prod)"
}

variable "vpc_cidr" {
  type        = string
  default     = "10.1.0.0/16"
  description = "VPC CIDR block"
}

variable "domain_name" {
  type        = string
  description = "Route53 hosted zone domain name"
}

variable "cloudfront_secret" {
  type        = string
  sensitive   = true
  description = "Shared secret for CloudFront → ALB origin validation"
}
