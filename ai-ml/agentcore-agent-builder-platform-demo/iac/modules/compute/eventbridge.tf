################################################################################
# EventBridge — CloudWatch Alarm → Platform API
################################################################################

resource "aws_cloudwatch_event_connection" "platform" {
  name               = "${var.prefix}-platform-conn"
  authorization_type = "API_KEY"

  auth_parameters {
    api_key {
      key   = "x-api-source"
      value = "eventbridge"
    }
  }
}

resource "aws_cloudwatch_event_api_destination" "platform_alarm" {
  name                             = "${var.prefix}-alarm-api-dest"
  invocation_endpoint              = var.domain_name != "" ? "https://aiops-v2.${var.domain_name}/api/events/alarm" : "https://${var.platform_domain}/api/events/alarm"
  http_method                      = "POST"
  invocation_rate_limit_per_second = 10

  connection_arn = aws_cloudwatch_event_connection.platform.arn
}

resource "aws_cloudwatch_event_rule" "alarm_to_agent" {
  name        = "${var.prefix}-alarm-to-agent"
  description = "CloudWatch Alarm ALARM state -> Platform API -> Incident Agent RCA"

  event_pattern = jsonencode({
    source      = ["aws.cloudwatch"]
    detail-type = ["CloudWatch Alarm State Change"]
    detail = {
      state = {
        value = ["ALARM"]
      }
    }
  })

  tags = var.tags
}

# Dead-letter queue for failed alarm deliveries. Without it, a failed delivery
# (e.g. a transient 401 that de-authorizes the API_KEY connection) is dropped
# silently and the incident is lost. With a DLQ + the alarm below, such
# failures are captured and visible.
resource "aws_sqs_queue" "alarm_dlq" {
  name                      = "${var.prefix}-alarm-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
  tags                      = var.tags
}

resource "aws_sqs_queue_policy" "alarm_dlq" {
  queue_url = aws_sqs_queue.alarm_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.alarm_dlq.arn
      Condition = {
        ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.alarm_to_agent.arn }
      }
    }]
  })
}

resource "aws_cloudwatch_event_target" "alarm_api" {
  rule     = aws_cloudwatch_event_rule.alarm_to_agent.name
  arn      = aws_cloudwatch_event_api_destination.platform_alarm.arn
  role_arn = aws_iam_role.eventbridge_api.arn

  dead_letter_config {
    arn = aws_sqs_queue.alarm_dlq.arn
  }

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 4
  }

  http_target {
    path_parameter_values   = []
    header_parameters       = {}
    query_string_parameters = {}
  }
}

# Surface delivery failures (e.g. a de-authorized connection) instead of losing
# incidents silently.
resource "aws_cloudwatch_metric_alarm" "alarm_delivery_failed" {
  alarm_name          = "${var.prefix}-alarm-delivery-failed"
  alarm_description   = "EventBridge failed to deliver a CloudWatch alarm to the incident endpoint (check connection auth)."
  namespace           = "AWS/Events"
  metric_name         = "FailedInvocations"
  dimensions          = { RuleName = aws_cloudwatch_event_rule.alarm_to_agent.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  tags                = var.tags
}

################################################################################
# EventBridge → API Destination Role (co-located to avoid circular deps)
################################################################################

resource "aws_iam_role" "eventbridge_api" {
  name = "${var.prefix}-eventbridge-api"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "eventbridge_api" {
  name = "${var.prefix}-eventbridge-api-policy"
  role = aws_iam_role.eventbridge_api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeApiDestination"
        Effect   = "Allow"
        Action   = "events:InvokeApiDestination"
        Resource = aws_cloudwatch_event_api_destination.platform_alarm.arn
      }
    ]
  })
}
