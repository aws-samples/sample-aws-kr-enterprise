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
        Sid    = "DynamoDBKMS"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = var.dynamodb_kms_key_arn
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
        Sid    = "BedrockAgentCoreControl"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateAgentRuntime",
          "bedrock-agentcore:CreateAgentRuntimeEndpoint",
          "bedrock-agentcore:GetAgentRuntime",
          "bedrock-agentcore:GetAgentRuntimeEndpoint",
          "bedrock-agentcore:ListAgentRuntimes",
          "bedrock-agentcore:DeleteAgentRuntime",
          "bedrock-agentcore:InvokeAgentRuntime"
        ]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.account_id}:runtime/*",
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.account_id}:runtime-endpoint/*"
        ]
      },
      {
        # List/Get gateway operations are account-scoped and do not support
        # resource-level constraints, so they require Resource = "*".
        Sid    = "BedrockAgentCoreGatewayList"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:ListGateways",
          "bedrock-agentcore:ListGatewayTargets",
          "bedrock-agentcore:GetGateway"
        ]
        Resource = "*"
      },
      {
        # CreateAgentRuntime implicitly provisions a workload identity in the
        # account's default directory, so deploying an agent from the web UI
        # (Agent Builder) requires workload-identity management permissions.
        Sid    = "BedrockAgentCoreWorkloadIdentity"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateWorkloadIdentity",
          "bedrock-agentcore:GetWorkloadIdentity",
          "bedrock-agentcore:UpdateWorkloadIdentity",
          "bedrock-agentcore:DeleteWorkloadIdentity",
          "bedrock-agentcore:ListWorkloadIdentities"
        ]
        Resource = "arn:aws:bedrock-agentcore:${var.aws_region}:${var.account_id}:workload-identity-directory/default*"
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
