################################################################################
# Platform API Task Role
################################################################################

resource "aws_iam_role" "platform_api_task" {
  name = "${var.prefix}-platform-api-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.prefix}-platform-api-task"
  })
}

resource "aws_iam_role_policy" "platform_api_task" {
  name = "${var.prefix}-platform-api-task-policy"
  role = aws_iam_role.platform_api_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBCRUD"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchGetItem",
          "dynamodb:BatchWriteItem"
        ]
        Resource = [
          var.platform_table_arn,
          "${var.platform_table_arn}/index/*",
          var.incidents_table_arn,
          "${var.incidents_table_arn}/index/*"
        ]
      },
      {
        Sid    = "BedrockInvokeModel"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = [
          "arn:aws:bedrock:${var.aws_region}::foundation-model/*",
          "arn:aws:bedrock:*:${var.account_id}:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*"
        ]
      },
      {
        Sid      = "BedrockAgentCoreControl"
        Effect   = "Allow"
        Action   = "bedrock-agentcore:*"
        Resource = "*"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/ecs/${var.prefix}*"
      },
      {
        Sid      = "PassRoleForAgentCore"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.agentcore_runtime.arn
      },
      {
        Sid    = "CreateServiceLinkedRole"
        Effect = "Allow"
        Action = "iam:CreateServiceLinkedRole"
        Resource = "arn:aws:iam::${var.account_id}:role/aws-service-role/*bedrock*/*"
      },
      {
        Sid    = "XRayReadOnly"
        Effect = "Allow"
        Action = [
          "xray:GetTraceSummaries",
          "xray:BatchGetTraces",
          "xray:GetServiceGraph",
          "xray:GetTraceGraph"
        ]
        Resource = "*"
      },
      {
        Sid    = "CloudWatchLogsRead"
        Effect = "Allow"
        Action = [
          "logs:StartQuery",
          "logs:GetQueryResults",
          "logs:FilterLogEvents",
          "logs:DescribeLogGroups",
          "logs:GetLogEvents"
        ]
        Resource = [
          "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:aws/spans:*",
          "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/bedrock-agentcore/*"
        ]
      },
      {
        Sid    = "CloudWatchMetricsRead"
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricData",
          "cloudwatch:ListMetrics"
        ]
        Resource = "*"
      }
    ]
  })
}
