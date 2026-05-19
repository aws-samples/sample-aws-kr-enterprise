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

output "cognito_user_pool_id" {
  value = module.auth.user_pool_id
}

output "cloudfront_distribution_id" {
  value = module.cdn.platform_distribution_id
}
