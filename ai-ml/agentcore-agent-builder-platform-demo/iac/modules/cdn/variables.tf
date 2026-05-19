variable "prefix" {
  type        = string
  description = "Resource name prefix"
}

variable "domain_name" {
  type        = string
  description = "Root domain name (e.g. my-awesome-app.xyz)"
}

variable "alb_dns" {
  type        = string
  description = "ALB DNS name (origin for platform distribution)"
}

variable "reports_bucket_regional_domain" {
  type        = string
  description = "S3 reports bucket regional domain name"
}

variable "reports_bucket_id" {
  type        = string
  description = "S3 reports bucket ID for bucket policy"
}

variable "reports_bucket_arn" {
  type        = string
  description = "S3 reports bucket ARN for bucket policy"
}

variable "acm_cert_arn_cloudfront" {
  type        = string
  description = "ACM certificate ARN in us-east-1 for CloudFront"
}

variable "cloudfront_secret" {
  type        = string
  sensitive   = true
  description = "Shared secret header value for origin validation"
}

variable "route53_zone_id" {
  type        = string
  description = "Route53 hosted zone ID"
}

variable "tags" {
  type        = map(string)
  description = "Common tags"
  default     = {}
}
