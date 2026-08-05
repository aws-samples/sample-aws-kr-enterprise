################################################################################
# AgentCore Runtime Role
################################################################################

resource "aws_iam_role" "agentcore_runtime" {
  name = "${var.prefix}-agentcore-runtime"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = [
            "bedrock.amazonaws.com",
            "bedrock-agentcore.amazonaws.com"
          ]
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.prefix}-agentcore-runtime"
  })
}

resource "aws_iam_role_policy" "agentcore_runtime" {
  name = "${var.prefix}-agentcore-runtime-policy"
  role = aws_iam_role.agentcore_runtime.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query"
        ]
        Resource = [
          var.platform_table_arn,
          "${var.platform_table_arn}/index/*",
          var.incidents_table_arn,
          "${var.incidents_table_arn}/index/*"
        ]
      },
      {
        # Least-privilege PutItem on the shared platform table. The runtime's
        # only legitimate write to this table is a Side-Channel event whose
        # partition key is SESSION#<id> (side_channel.py). Constraining
        # dynamodb:LeadingKeys to SESSION#* prevents an agent (e.g. a
        # prompt-injected dynamodb_put tool defaulting to DYNAMODB_TABLE) from
        # overwriting the AGENT#*/CONFIG or SUPERVISOR registry rows and
        # hijacking supervisor routing/prompts.
        Sid      = "DynamoDBSideChannelWrite"
        Effect   = "Allow"
        Action   = "dynamodb:PutItem"
        Resource = var.platform_table_arn
        Condition = {
          "ForAllValues:StringLike" = {
            "dynamodb:LeadingKeys" = ["SESSION#*"]
          }
        }
      },
      {
        # Incident records live in a separate table with their own key schema;
        # create_incident (dynamodb_put) writes here.
        Sid      = "DynamoDBIncidentWrite"
        Effect   = "Allow"
        Action   = "dynamodb:PutItem"
        Resource = var.incidents_table_arn
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
        Sid      = "S3Reports"
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${var.reports_bucket_arn}/*"
      },
      {
        # REPORT_URL contract: the report agent signs report URLs with the
        # CloudFront private key stored by the cdn module in Secrets Manager
        # (secret name "${prefix}-reports-cf-signing-key"; Secrets Manager
        # appends a random 6-char suffix to the ARN, hence the trailing -*).
        Sid      = "SecretsManagerReportsSigningKey"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:${var.prefix}-reports-cf-signing-key-*"
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
          "arn:aws:bedrock:${var.aws_region}:${var.account_id}:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*"
        ]
      },
      {
        Sid    = "BedrockAgentCore"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeAgentRuntime",
          "bedrock-agentcore:GetAgentRuntime"
        ]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.account_id}:runtime/*",
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.account_id}:runtime-endpoint/*"
        ]
      },
      {
        # The agent runtime resolves gateway name -> real (suffixed) gatewayId
        # via list_gateways() (mcp_connector.py) before opening the MCP
        # connection. Without these, resolution silently falls back to the
        # bare name and the MCP endpoint returns HTTP 400. Account-scoped
        # list/get ops do not support resource-level constraints.
        Sid    = "BedrockAgentCoreGatewayDiscovery"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:ListGateways",
          "bedrock-agentcore:GetGateway",
          "bedrock-agentcore:ListGatewayTargets"
        ]
        Resource = "*"
      },
      {
        Sid    = "ECRPull"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchCheckLayerAvailability"
        ]
        Resource = "*"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = [
          "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/bedrock-agentcore/*",
          "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/bedrock-agentcore/*:*"
        ]
      },
      {
        Sid    = "OTELTracing"
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets"
        ]
        Resource = "*"
      }
    ]
  })
}
