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
        { name = "BASE_IMAGE_URI", value = "${var.ecr_repo_urls["base-image"]}:latest" },
        { name = "AGENTCORE_ROLE_ARN", value = var.agentcore_runtime_role_arn },
        { name = "COGNITO_USER_POOL_ID", value = var.cognito_user_pool_id },
        { name = "COGNITO_CLIENT_ID", value = var.cognito_client_id },
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

  tags = merge(var.tags, {
    Name = "${var.prefix}-frontend-svc"
  })
}
