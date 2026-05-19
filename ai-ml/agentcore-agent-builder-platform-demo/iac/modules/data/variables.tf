variable "prefix" {
  type        = string
  description = "Resource name prefix"
}

variable "tags" {
  type        = map(string)
  description = "Common tags"
  default     = {}
}
