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

      # -------------------------------------------------------------------
      # aws/spans span-storage log group (K2 — verified against a live account)
      #
      # The `aws/spans` group is created ONLY by the Transaction Search service
      # itself, at the moment the X-Ray trace-segment destination transitions
      # INTO CloudWatchLogs. It cannot be created any other way:
      #   aws logs create-log-group --log-group-name aws/spans
      #     -> InvalidParameterException: "Log groups starting with AWS/ are
      #        reserved for AWS."
      # So the previous approach (create-log-group with the error swallowed by
      # `|| true`) silently did nothing and the Trace Viewer stayed empty while
      # Transaction Search read ACTIVE.
      #
      # Fix: if the group is missing we must TRIGGER a fresh destination
      # transition. When the destination is already CloudWatchLogs, setting it
      # again is a no-op (and errors), so we first flip to XRay and then back to
      # CloudWatchLogs. Each transition passes through PENDING and must reach
      # ACTIVE before the next call is accepted (otherwise: InvalidRequestException
      # "Updates are not allowed while the current status is PENDING").
      # -------------------------------------------------------------------

      wait_active() {
        # Poll until the destination status is ACTIVE (up to ~5 min).
        for _ in $(seq 1 30); do
          st=$(aws xray get-trace-segment-destination --region "$REGION" \
                 --query 'Status' --output text 2>/dev/null || echo "")
          [ "$st" = "ACTIVE" ] && return 0
          sleep 10
        done
        echo "WARN: trace-segment destination did not reach ACTIVE in time" >&2
        return 1
      }

      spans_group_exists() {
        found=$(aws logs describe-log-groups --log-group-name-prefix "aws/spans" \
                  --region "$REGION" \
                  --query "logGroups[?logGroupName=='aws/spans'] | length(@)" \
                  --output text 2>/dev/null || echo "0")
        [ "$found" = "1" ]
      }

      CURRENT=$(aws xray get-trace-segment-destination --region "$REGION" \
        --query 'Destination' --output text 2>/dev/null || echo "")

      if spans_group_exists; then
        # Group present — just make sure the destination is CloudWatchLogs.
        if [ "$CURRENT" != "CloudWatchLogs" ]; then
          wait_active || true
          aws xray update-trace-segment-destination \
            --destination CloudWatchLogs --region "$REGION"
          wait_active || true
        else
          echo "Transaction Search already enabled; aws/spans present."
        fi
      else
        # Group missing — force the service to auto-create it via a full
        # transition into CloudWatchLogs.
        echo "aws/spans missing — cycling trace-segment destination to recreate it."
        wait_active || true
        if [ "$CURRENT" = "CloudWatchLogs" ]; then
          # Already CloudWatchLogs: flip to XRay first so the switch back is a
          # real INTO-CloudWatchLogs transition that recreates the group.
          aws xray update-trace-segment-destination --destination XRay --region "$REGION"
          wait_active || true
        fi
        aws xray update-trace-segment-destination \
          --destination CloudWatchLogs --region "$REGION"
        wait_active || true
      fi

      # 100% sampling so the demo's Trace Viewer shows every request.
      aws xray update-indexing-rule \
        --name Default \
        --rule '{"Probabilistic":{"DesiredSamplingPercentage":100}}' \
        --region "$REGION"

      # Best-effort retention on the service-managed group (ignore if the
      # service has not finished materializing it yet — a later apply heals it).
      aws logs put-retention-policy --log-group-name "aws/spans" \
        --retention-in-days 30 --region "$REGION" 2>/dev/null || true
    EOT
  }
}
