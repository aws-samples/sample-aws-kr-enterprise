variable "aws_region" {
  type        = string
  description = "AWS region"
}

variable "account_id" {
  type        = string
  description = "AWS account ID"
}

variable "tags" {
  type        = map(string)
  description = "Common tags"
  default     = {}
}
