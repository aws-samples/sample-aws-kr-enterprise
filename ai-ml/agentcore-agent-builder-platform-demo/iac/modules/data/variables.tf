variable "prefix" {
  type        = string
  description = "Resource name prefix"
}

variable "account_id" {
  type        = string
  description = "AWS Account ID for globally unique resource naming"
}

variable "tags" {
  type        = map(string)
  description = "Common tags"
  default     = {}
}
