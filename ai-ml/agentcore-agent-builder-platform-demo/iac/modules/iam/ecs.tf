################################################################################
# ECS Execution Role
################################################################################

resource "aws_iam_role" "ecs_execution" {
  name = "${var.prefix}-ecs-execution"

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
    Name = "${var.prefix}-ecs-execution"
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The platform-api task definition injects the EventBridge shared secret via a
# `secrets` (valueFrom) reference to an SSM SecureString. Secret resolution is
# performed by the EXECUTION role at task start (not the task role), so grant it
# read access to that one parameter. The SecureString uses the AWS-managed key
# alias/aws/ssm, which does not require an explicit kms:Decrypt grant here.
resource "aws_iam_role_policy" "ecs_execution_ssm_secrets" {
  name = "${var.prefix}-ecs-execution-ssm-secrets"
  role = aws_iam_role.ecs_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadEventBridgeSecret"
        Effect   = "Allow"
        Action   = ["ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.aws_region}:${var.account_id}:parameter/${var.prefix}/eventbridge/api-source-secret"
      }
    ]
  })
}
