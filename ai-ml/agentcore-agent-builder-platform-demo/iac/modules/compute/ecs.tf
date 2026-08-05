################################################################################
# ECS Cluster
################################################################################

resource "aws_ecs_cluster" "main" {
  name = "${var.prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-cluster"
  })
}

################################################################################
# CloudWatch Log Groups
################################################################################

resource "aws_cloudwatch_log_group" "platform_api" {
  name              = "/ecs/${var.prefix}/platform-api"
  retention_in_days = 365

  tags = merge(var.tags, {
    Name = "${var.prefix}-platform-api-logs"
  })
}

resource "aws_cloudwatch_log_group" "frontend" {
  name              = "/ecs/${var.prefix}/frontend"
  retention_in_days = 365

  tags = merge(var.tags, {
    Name = "${var.prefix}-frontend-logs"
  })
}

################################################################################
# Platform API — Task Definition + Service
################################################################################

resource "aws_ecs_task_definition" "platform_api" {
  family                   = "${var.prefix}-platform-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.platform_api_task_role_arn

  container_definitions = jsonencode([
    {
      name      = "platform-api"
      image     = "${var.ecr_repo_urls["platform-api"]}:latest"
      essential = true
      portMappings = [
        {
          containerPort = 8000
          hostPort      = 8000
          protocol      = "tcp"
        }
      ]
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "DYNAMODB_TABLE", value = var.platform_table_name },
        { name = "INCIDENTS_TABLE", value = var.incidents_table_name },
        { name = "BASE_IMAGE_URI", value = "${var.ecr_repo_urls["base-image"]}:latest" },
        { name = "AGENTCORE_ROLE_ARN", value = var.agentcore_runtime_role_arn },
        { name = "COGNITO_USER_POOL_ID", value = var.cognito_user_pool_id },
        { name = "COGNITO_CLIENT_ID", value = var.cognito_client_id },
        # Propagated to agent runtimes (report agent writes reports here).
        { name = "REPORT_BUCKET", value = var.reports_bucket_name },
        { name = "REPORT_CF_DOMAIN", value = var.reports_cf_domain },
      ]
      # EVENTBRIDGE_SECRET contract: the ECS agent resolves the SSM SecureString
      # value at task start and injects it as the plain env var events.py reads
      # (EVENTBRIDGE_SECRET). This keeps the secret out of the task definition
      # JSON and matches the same value EventBridge presents in the x-api-source
      # header. Requires ssm:GetParameters (+ kms:Decrypt) on the execution role.
      secrets = [
        { name = "EVENTBRIDGE_SECRET", valueFrom = aws_ssm_parameter.eventbridge_secret.arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.platform_api.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "api"
        }
      }
      readonlyRootFilesystem = true
      mountPoints = [
        { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }
      ]
    }
  ])

  volume {
    name = "tmp"
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-platform-api-task"
  })
}

resource "aws_ecs_service" "platform_api" {
  name            = "${var.prefix}-platform-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.platform_api.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.platform_api.arn
    container_name   = "platform-api"
    container_port   = 8000
  }

  # CreateService requires the target group to already be associated with a load
  # balancer. Without this, a clean apply can create the service before the
  # listener attaches the TGs to the ALB, failing with "target group ... does
  # not have an associated load balancer." The API traffic flows via the
  # listener rule, so depend on it (which transitively depends on the listener).
  depends_on = [aws_lb_listener_rule.api]

  tags = merge(var.tags, {
    Name = "${var.prefix}-platform-api-svc"
  })
}

################################################################################
# Frontend — Task Definition + Service
################################################################################

resource "aws_ecs_task_definition" "frontend" {
  family                   = "${var.prefix}-frontend"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = var.execution_role_arn

  container_definitions = jsonencode([
    {
      name      = "frontend"
      image     = "${var.ecr_repo_urls["frontend"]}:latest"
      essential = true
      portMappings = [
        {
          containerPort = 3000
          hostPort      = 3000
          protocol      = "tcp"
        }
      ]
      environment = [
        { name = "PLATFORM_API_URL", value = "https://${aws_lb.main.dns_name}" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.frontend.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "fe"
        }
      }
      readonlyRootFilesystem = true
      mountPoints = [
        { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
        { sourceVolume = "next-cache", containerPath = "/app/.next/cache", readOnly = false }
      ]
    }
  ])

  volume {
    name = "tmp"
  }
  volume {
    name = "next-cache"
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-frontend-task"
  })
}

resource "aws_ecs_service" "frontend" {
  name            = "${var.prefix}-frontend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.frontend.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.frontend.arn
    container_name   = "frontend"
    container_port   = 3000
  }

  # The frontend TG is the listener's default action, so it is attached to the
  # ALB only once the listener exists. Depend on it to avoid the same
  # "target group ... does not have an associated load balancer" first-apply
  # failure described above.
  depends_on = [aws_lb_listener.http]

  tags = merge(var.tags, {
    Name = "${var.prefix}-frontend-svc"
  })
}
