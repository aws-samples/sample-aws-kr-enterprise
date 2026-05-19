variable "prefix" {
  type        = string
  description = "Resource name prefix"
}

variable "callback_urls" {
  type        = list(string)
  description = "OAuth2 callback URLs for Cognito client"
}

variable "logout_urls" {
  type        = list(string)
  description = "Logout URLs for Cognito client"
}

variable "tags" {
  type        = map(string)
  description = "Common tags"
  default     = {}
}
