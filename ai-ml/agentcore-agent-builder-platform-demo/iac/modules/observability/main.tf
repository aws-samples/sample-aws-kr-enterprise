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

resource "aws_cloudwatch_log_group" "spans" {
  name              = "aws/spans"
  retention_in_days = 30

  tags = merge(var.tags, {
    Name = "aws-spans"
  })
}

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
    aws_cloudwatch_log_group.spans,
  ]

  # Re-run if the region changes; the CLI calls are idempotent so re-applies are safe.
  triggers = {
    aws_region = var.aws_region
    account_id = var.account_id
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -eu
      REGION="${var.aws_region}"
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
