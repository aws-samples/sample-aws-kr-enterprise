output "spans_log_group_name" {
  # The group itself is auto-created by X-Ray when Transaction Search is enabled
  # (AWS reserves the `aws/` prefix, so it is not a Terraform-managed resource).
  value       = "aws/spans"
  description = "Name of the Transaction Search spans log group (auto-created by X-Ray)"
}
