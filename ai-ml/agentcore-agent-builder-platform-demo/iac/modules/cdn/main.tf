################################################################################
# Cache Policies
################################################################################

resource "aws_cloudfront_cache_policy" "disabled" {
  name        = "${var.prefix}-cache-disabled"
  comment     = "No caching - pass through all requests"
  min_ttl     = 0
  default_ttl = 0
  max_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

################################################################################
# Origin Request Policies
################################################################################

resource "aws_cloudfront_origin_request_policy" "all_viewer" {
  name    = "${var.prefix}-all-viewer"
  comment = "Forward all viewer headers, cookies, query strings to origin"

  cookies_config {
    cookie_behavior = "all"
  }
  headers_config {
    header_behavior = "allViewer"
  }
  query_strings_config {
    query_string_behavior = "all"
  }
}

################################################################################
# CloudFront VPC Origin (Internal ALB)
################################################################################

resource "aws_cloudfront_vpc_origin" "alb" {
  vpc_origin_endpoint_config {
    name                   = "${var.prefix}-alb-origin"
    arn                    = var.alb_arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"

    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-vpc-origin"
  })
}

################################################################################
# ALB SG Ingress — allow CloudFront VPC Origin managed SG
################################################################################

data "aws_security_group" "cloudfront_vpc_origin" {
  vpc_id = var.vpc_id

  filter {
    name   = "group-name"
    values = ["CloudFront-VPCOrigins-Service-SG"]
  }

  depends_on = [aws_cloudfront_vpc_origin.alb]
}

resource "aws_security_group_rule" "alb_ingress_vpc_origin" {
  type                     = "ingress"
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
  security_group_id        = var.alb_security_group_id
  source_security_group_id = data.aws_security_group.cloudfront_vpc_origin.id
  description              = "HTTP from CloudFront VPC Origin"
}

################################################################################
# CloudFront — Platform Distribution (ALB Origin)
################################################################################

resource "aws_cloudfront_distribution" "main" {
  enabled         = true
  comment         = "${var.prefix} platform"
  aliases         = var.domain_name != "" ? ["aiops-v2.${var.domain_name}"] : []
  is_ipv6_enabled = true

  origin {
    domain_name = var.alb_dns
    origin_id   = "alb"

    vpc_origin_config {
      vpc_origin_id = aws_cloudfront_vpc_origin.alb.id
    }
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "alb"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    cache_policy_id          = aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.all_viewer.id
  }

  ordered_cache_behavior {
    path_pattern           = "/api/*"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "alb"
    viewer_protocol_policy = "redirect-to-https"
    compress               = false

    cache_policy_id          = aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.all_viewer.id
  }

  dynamic "viewer_certificate" {
    for_each = var.domain_name != "" ? [1] : []
    content {
      acm_certificate_arn      = var.acm_cert_arn_cloudfront
      ssl_support_method       = "sni-only"
      minimum_protocol_version = "TLSv1.2_2021"
    }
  }

  dynamic "viewer_certificate" {
    for_each = var.domain_name != "" ? [] : [1]
    content {
      cloudfront_default_certificate = true
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-cf-platform"
  })
}

################################################################################
# CloudFront — Reports Distribution (S3 OAC Origin)
################################################################################

resource "aws_cloudfront_origin_access_control" "reports" {
  name                              = "${var.prefix}-reports-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "reports" {
  enabled         = true
  comment         = "${var.prefix} reports"
  is_ipv6_enabled = true

  origin {
    domain_name              = var.reports_bucket_regional_domain
    origin_id                = "s3-reports"
    origin_access_control_id = aws_cloudfront_origin_access_control.reports.id
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "s3-reports"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-cf-reports"
  })
}

################################################################################
# S3 Bucket Policy for Reports OAC
################################################################################

resource "aws_s3_bucket_policy" "reports" {
  bucket = var.reports_bucket_id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowCloudFrontOAC"
        Effect = "Allow"
        Principal = {
          Service = "cloudfront.amazonaws.com"
        }
        Action   = "s3:GetObject"
        Resource = "${var.reports_bucket_arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.reports.arn
          }
        }
      }
    ]
  })
}
