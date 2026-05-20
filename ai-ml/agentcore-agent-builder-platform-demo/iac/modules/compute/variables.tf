variable "prefix" {
  type        = string
  description = "Resource name prefix"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
}

variable "vpc_id" {
  type        = string
  description = "VPC ID"
}

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR for security group rules"
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "Public subnet IDs for ALB"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs for ECS tasks"
}

variable "ecr_repo_urls" {
  type        = map(string)
  description = "Map of ECR repository URLs (key = repo name)"
}

variable "execution_role_arn" {
  type        = string
  description = "ECS execution role ARN"
}

variable "platform_api_task_role_arn" {
  type        = string
  description = "Platform API task role ARN"
}

variable "agentcore_runtime_role_arn" {
  type        = string
  description = "AgentCore runtime role ARN"
}

variable "platform_table_name" {
  type        = string
  description = "DynamoDB platform table name"
}

variable "cognito_user_pool_id" {
  type        = string
  description = "Cognito User Pool ID for JWT verification"
}

variable "cognito_client_id" {
  type        = string
  description = "Cognito User Pool Client ID"
}

variable "cloudfront_secret" {
  type        = string
  sensitive   = true
  description = "Shared secret for CloudFront -> ALB header validation"
}

variable "domain_name" {
  type        = string
  description = "Platform domain name for EventBridge API destination"
}

variable "tags" {
  type        = map(string)
  description = "Common tags"
  default     = {}
}

variable "platform_domain" {
  type        = string
  description = "Platform domain (CloudFront distribution domain when no custom domain)"
  default     = ""
}
