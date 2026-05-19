################################################################################
# CloudFront Managed Prefix List
################################################################################

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

################################################################################
# ALB Security Group
################################################################################

resource "aws_security_group" "alb" {
  name_prefix = "${var.prefix}-alb-"
  vpc_id      = var.vpc_id
  description = "Security group for ALB — allows CloudFront only"

  ingress {
    description     = "HTTPS from CloudFront"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  ingress {
    description     = "HTTP from CloudFront"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    description = "To ECS tasks"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-alb-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

################################################################################
# ECS Security Group
################################################################################

resource "aws_security_group" "ecs" {
  name_prefix = "${var.prefix}-ecs-"
  vpc_id      = var.vpc_id
  description = "Security group for ECS tasks"

  ingress {
    description     = "Frontend from ALB"
    from_port       = 3000
    to_port         = 3000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description     = "Platform API from ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-ecs-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

################################################################################
# Application Load Balancer
################################################################################

resource "aws_lb" "main" {
  name               = "${var.prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids
  idle_timeout       = 300

  tags = merge(var.tags, {
    Name = "${var.prefix}-alb"
  })
}

################################################################################
# Target Groups
################################################################################

resource "aws_lb_target_group" "frontend" {
  name        = "${var.prefix}-frontend"
  port        = 3000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-frontend-tg"
  })
}

resource "aws_lb_target_group" "platform_api" {
  name        = "${var.prefix}-platform-api"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-platform-api-tg"
  })
}

################################################################################
# HTTP Listener (port 80) — default 403
################################################################################

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Forbidden"
      status_code  = "403"
    }
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-http-listener"
  })
}

################################################################################
# HTTP Listener Rules
################################################################################

resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.platform_api.arn
  }

  condition {
    path_pattern {
      values = ["/api/*"]
    }
  }

  condition {
    http_header {
      http_header_name = "X-CloudFront-Secret"
      values           = [var.cloudfront_secret]
    }
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-api-rule"
  })
}

resource "aws_lb_listener_rule" "frontend" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 200

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }

  condition {
    http_header {
      http_header_name = "X-CloudFront-Secret"
      values           = [var.cloudfront_secret]
    }
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-frontend-rule"
  })
}

################################################################################
# HTTPS Listener (port 443)
################################################################################

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_cert_arn

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Forbidden"
      status_code  = "403"
    }
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-https-listener"
  })
}

################################################################################
# HTTPS Listener Rules (Cognito auth)
################################################################################

resource "aws_lb_listener_rule" "https_api" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 100

  action {
    type = "authenticate-cognito"
    authenticate_cognito {
      user_pool_arn              = var.cognito_user_pool_arn
      user_pool_client_id        = var.cognito_client_id
      user_pool_domain           = var.cognito_domain
      session_cookie_name        = "AWSELBAuthSessionCookie"
      session_timeout            = 3600
      on_unauthenticated_request = "authenticate"
    }
    order = 1
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.platform_api.arn
    order            = 2
  }

  condition {
    path_pattern {
      values = ["/api/*"]
    }
  }

  condition {
    http_header {
      http_header_name = "X-CloudFront-Secret"
      values           = [var.cloudfront_secret]
    }
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-https-api-rule"
  })
}

resource "aws_lb_listener_rule" "https_frontend" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 200

  action {
    type = "authenticate-cognito"
    authenticate_cognito {
      user_pool_arn              = var.cognito_user_pool_arn
      user_pool_client_id        = var.cognito_client_id
      user_pool_domain           = var.cognito_domain
      session_cookie_name        = "AWSELBAuthSessionCookie"
      session_timeout            = 3600
      on_unauthenticated_request = "authenticate"
    }
    order = 1
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
    order            = 2
  }

  condition {
    http_header {
      http_header_name = "X-CloudFront-Secret"
      values           = [var.cloudfront_secret]
    }
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-https-frontend-rule"
  })
}
