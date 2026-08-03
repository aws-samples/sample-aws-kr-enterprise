output "spans_log_group_name" {
  value       = aws_cloudwatch_log_group.spans.name
  description = "Name of the Transaction Search spans log group"
}
