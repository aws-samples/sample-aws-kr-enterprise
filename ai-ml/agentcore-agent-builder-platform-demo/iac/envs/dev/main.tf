terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

locals {
  prefix = "${var.project}-${var.env}"
  common_tags = {
    Project     = var.project
    Environment = var.env
    ManagedBy   = "terraform"
  }
}

################################################################################
# Data Sources
################################################################################

data "aws_caller_identity" "current" {}

data "aws_acm_certificate" "alb" {
  domain      = "*.${var.domain_name}"
  statuses    = ["ISSUED"]
  most_recent = true
}

data "aws_acm_certificate" "cloudfront" {
  provider    = aws.us_east_1
  domain      = "*.${var.domain_name}"
  statuses    = ["ISSUED"]
  most_recent = true
}

data "aws_route53_zone" "main" {
  name = var.domain_name
}

################################################################################
# Modules
################################################################################

module "network" {
  source     = "../../modules/network"
  prefix     = local.prefix
  vpc_cidr   = var.vpc_cidr
  aws_region = var.aws_region
  tags       = local.common_tags
}

module "registry" {
  source = "../../modules/registry"
  prefix = local.prefix
  tags   = local.common_tags
}

module "data" {
  source = "../../modules/data"
  prefix = local.prefix
  tags   = local.common_tags
}

module "auth" {
  source = "../../modules/auth"
  prefix = local.prefix
  callback_urls = [
    "https://aiops-v2.${var.domain_name}/api/auth/callback",
    "https://aiops-v2.${var.domain_name}/oauth2/idpresponse",
  ]
  logout_urls = [
    "https://aiops-v2.${var.domain_name}",
  ]
  tags = local.common_tags
}

module "iam" {
  source              = "../../modules/iam"
  prefix              = local.prefix
  aws_region          = var.aws_region
  account_id          = data.aws_caller_identity.current.account_id
  platform_table_arn  = module.data.platform_table_arn
  incidents_table_arn = module.data.incidents_table_arn
  reports_bucket_arn  = module.data.reports_bucket_arn
  tags                = local.common_tags
}

module "compute" {
  source                     = "../../modules/compute"
  prefix                     = local.prefix
  aws_region                 = var.aws_region
  vpc_id                     = module.network.vpc_id
  vpc_cidr                   = module.network.vpc_cidr
  public_subnet_ids          = module.network.public_subnet_ids
  private_subnet_ids         = module.network.private_subnet_ids
  ecr_repo_urls              = module.registry.repo_urls
  execution_role_arn         = module.iam.ecs_execution_role_arn
  platform_api_task_role_arn = module.iam.platform_api_task_role_arn
  agentcore_runtime_role_arn = module.iam.agentcore_runtime_role_arn
  platform_table_name        = module.data.platform_table_name
  cognito_user_pool_arn      = module.auth.user_pool_arn
  cognito_client_id          = module.auth.client_id
  cognito_domain             = module.auth.domain
  acm_cert_arn               = data.aws_acm_certificate.alb.arn
  cloudfront_secret          = var.cloudfront_secret
  domain_name                = var.domain_name
  tags                       = local.common_tags
}

module "cdn" {
  source                         = "../../modules/cdn"
  prefix                         = local.prefix
  domain_name                    = var.domain_name
  alb_dns                        = module.compute.alb_dns
  reports_bucket_regional_domain = module.data.reports_bucket_regional_domain
  reports_bucket_id              = module.data.reports_bucket_name
  reports_bucket_arn             = module.data.reports_bucket_arn
  acm_cert_arn_cloudfront        = data.aws_acm_certificate.cloudfront.arn
  cloudfront_secret              = var.cloudfront_secret
  route53_zone_id                = data.aws_route53_zone.main.zone_id
  tags                           = local.common_tags
}
