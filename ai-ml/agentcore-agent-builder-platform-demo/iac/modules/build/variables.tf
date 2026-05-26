variable "prefix" {
  type        = string
  description = "Resource name prefix"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
}

variable "account_id" {
  type        = string
  description = "AWS account ID"
}

variable "ecr_repo_arns" {
  type        = map(string)
  description = "ECR repository ARNs from registry module"
}

variable "tags" {
  type        = map(string)
  description = "Common tags"
  default     = {}
}
