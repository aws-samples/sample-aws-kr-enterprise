terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    tls = {
      source = "hashicorp/tls"
    }
  }
}

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
  # checkov:skip=CKV_AWS_305:Origin is a dynamic Next.js app behind an internal ALB, not static S3. Setting default_root_object=index.html makes CloudFront rewrite "/" to a nonexistent /index.html and breaks the home route (Next.js serves "/" via app/page.tsx). Submitted for waiver.
  enabled         = true
  comment         = "${var.prefix} platform"
  aliases         = var.domain_name != "" ? ["aiops-v2.${var.domain_name}"] : []
  is_ipv6_enabled = true

  origin {
    domain_name = var.alb_dns
    origin_id   = "alb"

    vpc_origin_config {
      vpc_origin_id = aws_cloudfront_vpc_origin.alb.id
      # Extend the origin read timeout (default 30s, max 60s) so a long-lived
      # SSE stream (e.g. an agent blocked on a synchronous A2A delegation) is
      # not severed mid-response. Paired with a <10s SSE keepalive server-side.
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
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

  logging_config {
    bucket          = aws_s3_bucket.cf_logs.bucket_domain_name
    prefix          = "platform/"
    include_cookies = false
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

################################################################################
# Reports — CloudFront Signed URL provisioning (REPORT_URL contract)
#
# The reports distribution is NOT public: its default cache behavior is gated by
# a trusted key group, so CloudFront only serves requests carrying a valid
# signature. A key pair is generated at deploy time — the public key is uploaded
# to CloudFront (below) and the private key PEM is stored in Secrets Manager.
# The report agent runtime loads the private key from Secrets Manager and signs
# every returned report URL with a short expiry (see report_tools/s3_uploader.py,
# env REPORT_CF_KEY_PAIR_ID / REPORT_CF_PRIVATE_KEY_SECRET wired from the outputs
# of this module).
################################################################################

resource "tls_private_key" "reports_signing" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "aws_cloudfront_public_key" "reports" {
  name        = "${var.prefix}-reports-pubkey"
  comment     = "Public key for reports signed URLs"
  encoded_key = tls_private_key.reports_signing.public_key_pem
}

resource "aws_cloudfront_key_group" "reports" {
  name    = "${var.prefix}-reports-key-group"
  comment = "Trusted key group gating the reports distribution"
  items   = [aws_cloudfront_public_key.reports.id]
}

resource "aws_secretsmanager_secret" "reports_signing_key" {
  name        = "${var.prefix}-reports-cf-signing-key"
  description = "CloudFront private key (PEM) for signing reports URLs"
  tags        = var.tags
}

resource "aws_secretsmanager_secret_version" "reports_signing_key" {
  secret_id     = aws_secretsmanager_secret.reports_signing_key.id
  secret_string = tls_private_key.reports_signing.private_key_pem
}

resource "aws_cloudfront_distribution" "reports" {
  #checkov:skip=CKV_AWS_174:Uses cloudfront_default_certificate; minimum TLS version is fixed by CloudFront and cannot be set. Custom-domain path (main dist) enforces TLSv1.2_2021.
  enabled             = true
  comment             = "${var.prefix} reports"
  is_ipv6_enabled     = true
  default_root_object = "index.html"

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

    # Gate every report behind a CloudFront signed URL. CloudFront rejects any
    # request to this behavior that lacks a valid signature from the trusted
    # key group, removing the unauthenticated public read path (H9 / REPORT_URL).
    trusted_key_groups = [aws_cloudfront_key_group.reports.id]
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  logging_config {
    bucket          = aws_s3_bucket.cf_logs.bucket_domain_name
    prefix          = "reports/"
    include_cookies = false
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-cf-reports"
  })
}

################################################################################
# Reports signing-key outputs (REPORT_URL contract)
#
# Consumed by the report agent runtime env (deploy-agents.sh -> the runtime's
# REPORT_CF_KEY_PAIR_ID / REPORT_CF_PRIVATE_KEY_SECRET). Declared here (rather
# than outputs.tf) so the signed-URL contract stays self-contained.
################################################################################

output "reports_cf_key_pair_id" {
  description = "CloudFront public key (key pair) id used to sign reports URLs"
  value       = aws_cloudfront_public_key.reports.id
}

output "reports_cf_private_key_secret_name" {
  description = "Secrets Manager secret name holding the CloudFront signing private key PEM"
  value       = aws_secretsmanager_secret.reports_signing_key.name
}

output "reports_cf_private_key_secret_arn" {
  description = "Secrets Manager secret ARN holding the CloudFront signing private key PEM"
  value       = aws_secretsmanager_secret.reports_signing_key.arn
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
