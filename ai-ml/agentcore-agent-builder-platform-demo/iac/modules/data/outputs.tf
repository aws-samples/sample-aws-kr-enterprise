output "platform_table_name" {
  value = aws_dynamodb_table.platform.name
}

output "platform_table_arn" {
  value = aws_dynamodb_table.platform.arn
}

output "incidents_table_name" {
  value = aws_dynamodb_table.incidents.name
}

output "incidents_table_arn" {
  value = aws_dynamodb_table.incidents.arn
}

output "reports_bucket_name" {
  value = aws_s3_bucket.reports.bucket
}

output "reports_bucket_arn" {
  value = aws_s3_bucket.reports.arn
}

output "reports_bucket_regional_domain" {
  value = aws_s3_bucket.reports.bucket_regional_domain_name
}

output "dynamodb_kms_key_arn" {
  value = aws_kms_key.dynamodb.arn
}
