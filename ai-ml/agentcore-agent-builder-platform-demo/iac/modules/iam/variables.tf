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

variable "platform_table_arn" {
  type        = string
  description = "DynamoDB platform table ARN"
}

variable "incidents_table_arn" {
  type        = string
  description = "DynamoDB incidents table ARN"
}

variable "reports_bucket_arn" {
  type        = string
  description = "S3 reports bucket ARN"
}

variable "tags" {
  type        = map(string)
  description = "Common tags"
  default     = {}
}
