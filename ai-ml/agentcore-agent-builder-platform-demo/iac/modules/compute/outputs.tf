output "alb_dns" {
  value = aws_lb.main.dns_name
}

output "alb_arn" {
  value = aws_lb.main.arn
}

output "ecs_cluster_arn" {
  value = aws_ecs_cluster.main.arn
}

output "eventbridge_api_destination_arn" {
  value = aws_cloudwatch_event_api_destination.platform_alarm.arn
}
