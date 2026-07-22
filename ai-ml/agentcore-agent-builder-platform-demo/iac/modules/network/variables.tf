variable "prefix" {
  type        = string
  description = "Resource name prefix (e.g. aiops-v2-dev)"
}

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR block"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
}

variable "tags" {
  type        = map(string)
  description = "Common tags for all resources"
  default     = {}
}
