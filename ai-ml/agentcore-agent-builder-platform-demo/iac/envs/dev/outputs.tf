output "vpc_id" {
  value = module.network.vpc_id
}

output "ecs_cluster_arn" {
  value = module.compute.ecs_cluster_arn
}

output "alb_dns" {
  value = module.compute.alb_dns
}

output "dynamodb_platform_table" {
  value = module.data.platform_table_name
}

output "dynamodb_incidents_table" {
  value = module.data.incidents_table_name
}

output "ecr_repos" {
  value = module.registry.repo_urls
}

output "s3_reports_bucket" {
  value = module.data.reports_bucket_name
}

output "reports_distribution_domain" {
  value = module.cdn.reports_distribution_domain
}

# REPORT_URL contract: consumed by deploy-agents.sh to inject the report agent's
# CloudFront signing env (REPORT_CF_KEY_PAIR_ID / REPORT_CF_PRIVATE_KEY_SECRET).
output "reports_cf_key_pair_id" {
  value = module.cdn.reports_cf_key_pair_id
}

output "reports_cf_private_key_secret_name" {
  value = module.cdn.reports_cf_private_key_secret_name
}

output "cognito_user_pool_id" {
  value = module.auth.user_pool_id
}

output "cloudfront_distribution_id" {
  value = module.cdn.platform_distribution_id
}

output "codebuild_project_arm64" {
  value = module.build.codebuild_project_arm64
}

output "codebuild_project_x86" {
  value = module.build.codebuild_project_x86
}

output "codebuild_source_bucket" {
  value = module.build.source_bucket
}
