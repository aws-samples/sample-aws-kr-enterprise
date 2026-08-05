################################################################################
# CloudWatch Transaction Search
#
# Reproduces the manual enablement sequence required by the demo's Trace Viewer:
#   1. Log group `aws/spans` (30-day retention).
#   2. CloudWatch Logs resource policy `TransactionSearchAccess` allowing
#      xray.amazonaws.com to PutLogEvents into the spans / application-signals
#      log groups (both aws:SourceArn ArnLike + aws:SourceAccount conditions —
#      the missing ArnLike condition was the exact cause of an AccessDenied).
#   3. `aws xray update-trace-segment-destination --destination CloudWatchLogs`
#   4. `aws xray update-indexing-rule --name Default \
#         --rule '{"Probabilistic":{"DesiredSamplingPercentage":100}}'`
#
# Steps 3-4 have no native Terraform resource in the AWS provider, so they run
# via a null_resource local-exec provisioner (AWS CLI required on deploy host).
################################################################################

# 1. Spans log group ----------------------------------------------------------
#
# NOTE: The `aws/spans` log group is NOT created via aws_cloudwatch_log_group.
# The Terraform provider rejects the reserved `aws/` prefix with
# InvalidParameterException. Per the Transaction Search docs the group is created
# by the service when X-Ray's trace segment destination is switched to
# CloudWatchLogs, and the enabling principal must hold logs:CreateLogGroup on
# `aws/spans`. Because the destination-switch provisioner below is a no-op once
# the destination is already CloudWatchLogs, a group that was deleted
# out-of-band (the K2 symptom: empty Trace Viewer while Transaction Search is
# ACTIVE) would never be recreated. The provisioner below therefore explicitly
# ensures the group exists on every apply, independent of the destination state.
# Refs:
#   https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Enable-TransactionSearch.html
#   https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html

# 2. Transaction Search resource policy ---------------------------------------

data "aws_iam_policy_document" "transaction_search" {
  statement {
    sid    = "TransactionSearchAccess"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["xray.amazonaws.com"]
    }

    actions = ["logs:PutLogEvents"]

    resources = [
      "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:aws/spans:*",
      "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/application-signals/data:*",
    ]

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:xray:${var.aws_region}:${var.account_id}:*"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "transaction_search" {
  policy_name     = "TransactionSearchAccess"
  policy_document = data.aws_iam_policy_document.transaction_search.json
}

# 3-4. Enable X-Ray Transaction Search (CLI — no native TF resource) ----------

resource "null_resource" "enable_transaction_search" {
  depends_on = [
    aws_cloudwatch_log_resource_policy.transaction_search,
  ]

  # Re-run on every apply so an out-of-band deletion of the aws/spans group is
  # healed (the K2 root cause). All CLI calls here are idempotent, so re-runs
  # are safe.
  triggers = {
    aws_region = var.aws_region
    account_id = var.account_id
    always_run = timestamp()
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -eu
      REGION="${var.aws_region}"
      # Ensure the aws/spans span-storage log group exists. Transaction Search
      # normally auto-creates it on the destination switch, but if it was
      # deleted out-of-band the switch is a no-op and the Trace Viewer stays
      # empty (K2). CreateLogGroup is idempotent-safe here: swallow the
      # ResourceAlreadyExistsException so re-applies don't fail.
      aws logs create-log-group --log-group-name "aws/spans" --region "$REGION" 2>/dev/null || true
      aws logs put-retention-policy --log-group-name "aws/spans" \
        --retention-in-days 30 --region "$REGION" 2>/dev/null || true

      # Idempotent: only switch the destination if it isn't already CloudWatchLogs
      # (a re-run otherwise raises InvalidRequestException "already set").
      CURRENT=$(aws xray get-trace-segment-destination --region "$REGION" \
        --query 'Destination' --output text 2>/dev/null || echo "")
      if [ "$CURRENT" != "CloudWatchLogs" ]; then
        aws xray update-trace-segment-destination \
          --destination CloudWatchLogs --region "$REGION"
      else
        echo "Transaction Search already enabled (destination=CloudWatchLogs)"
      fi
      aws xray update-indexing-rule \
        --name Default \
        --rule '{"Probabilistic":{"DesiredSamplingPercentage":100}}' \
        --region "$REGION"
    EOT
  }
}
