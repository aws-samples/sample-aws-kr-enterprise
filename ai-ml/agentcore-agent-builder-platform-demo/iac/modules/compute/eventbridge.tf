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
  invocation_endpoint              = "https://aiops-v2.${var.domain_name}/api/events/alarm"
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

resource "aws_cloudwatch_event_target" "alarm_api" {
  rule     = aws_cloudwatch_event_rule.alarm_to_agent.name
  arn      = aws_cloudwatch_event_api_destination.platform_alarm.arn
  role_arn = aws_iam_role.eventbridge_api.arn

  http_target {
    path_parameter_values   = []
    header_parameters       = {}
    query_string_parameters = {}
  }
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
