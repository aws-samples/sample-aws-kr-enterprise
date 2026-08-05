output "platform_distribution_id" {
  value = aws_cloudfront_distribution.main.id
}

output "platform_distribution_domain" {
  value = aws_cloudfront_distribution.main.domain_name
}

output "reports_distribution_id" {
  value = aws_cloudfront_distribution.reports.id
}

output "reports_distribution_arn" {
  value = aws_cloudfront_distribution.reports.arn
}

output "reports_distribution_domain" {
  value = aws_cloudfront_distribution.reports.domain_name
}
