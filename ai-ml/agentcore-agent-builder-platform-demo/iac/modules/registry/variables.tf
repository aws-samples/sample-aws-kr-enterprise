variable "prefix" {
  type        = string
  description = "Resource name prefix"
}

variable "repo_names" {
  type        = set(string)
  description = "ECR repository names"
  default     = ["base-image", "report-image", "platform-api", "frontend"]
}

variable "max_image_count" {
  type        = number
  description = "Max images to retain per repo"
  default     = 5
}

variable "tags" {
  type        = map(string)
  description = "Common tags"
  default     = {}
}
