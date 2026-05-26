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
  prefix            = "${var.project}-${var.env}"
  use_custom_domain = var.domain_name != ""
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

data "aws_acm_certificate" "cloudfront" {
  count       = local.use_custom_domain ? 1 : 0
  provider    = aws.us_east_1
  domain      = "*.${var.domain_name}"
  statuses    = ["ISSUED"]
  most_recent = true
}

data "aws_route53_zone" "main" {
  count = local.use_custom_domain ? 1 : 0
  name  = var.domain_name
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
  source     = "../../modules/data"
  prefix     = local.prefix
  account_id = data.aws_caller_identity.current.account_id
  tags       = local.common_tags
}

module "auth" {
  source = "../../modules/auth"
  prefix = local.prefix
  callback_urls = local.use_custom_domain ? [
    "https://aiops-v2.${var.domain_name}/api/auth/callback",
    "https://aiops-v2.${var.domain_name}/oauth2/idpresponse",
    ] : [
    "https://${module.cdn.platform_distribution_domain}/api/auth/callback",
    "https://${module.cdn.platform_distribution_domain}/oauth2/idpresponse",
  ]
  logout_urls = local.use_custom_domain ? [
    "https://aiops-v2.${var.domain_name}",
    ] : [
    "https://${module.cdn.platform_distribution_domain}",
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
  private_subnet_ids         = module.network.private_subnet_ids
  ecr_repo_urls              = module.registry.repo_urls
  execution_role_arn         = module.iam.ecs_execution_role_arn
  platform_api_task_role_arn = module.iam.platform_api_task_role_arn
  agentcore_runtime_role_arn = module.iam.agentcore_runtime_role_arn
  platform_table_name        = module.data.platform_table_name
  cognito_user_pool_id       = module.auth.user_pool_id
  cognito_client_id          = module.auth.client_id
  domain_name                = var.domain_name
  platform_domain            = module.cdn.platform_distribution_domain
  tags                       = local.common_tags
}

module "cdn" {
  source                         = "../../modules/cdn"
  prefix                         = local.prefix
  domain_name                    = var.domain_name
  alb_dns                        = module.compute.alb_dns
  alb_arn                        = module.compute.alb_arn
  vpc_id                         = module.network.vpc_id
  alb_security_group_id          = module.compute.alb_security_group_id
  reports_bucket_regional_domain = module.data.reports_bucket_regional_domain
  reports_bucket_id              = module.data.reports_bucket_name
  reports_bucket_arn             = module.data.reports_bucket_arn
  acm_cert_arn_cloudfront        = local.use_custom_domain ? data.aws_acm_certificate.cloudfront[0].arn : ""
  route53_zone_id                = local.use_custom_domain ? data.aws_route53_zone.main[0].zone_id : ""
  tags                           = local.common_tags
}

module "build" {
  source        = "../../modules/build"
  prefix        = local.prefix
  aws_region    = var.aws_region
  account_id    = data.aws_caller_identity.current.account_id
  ecr_repo_arns = module.registry.repo_arns
  tags          = local.common_tags
}
