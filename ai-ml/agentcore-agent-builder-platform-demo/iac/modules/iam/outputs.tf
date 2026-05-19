output "ecs_execution_role_arn" {
  value = aws_iam_role.ecs_execution.arn
}

output "platform_api_task_role_arn" {
  value = aws_iam_role.platform_api_task.arn
}

output "agentcore_runtime_role_arn" {
  value = aws_iam_role.agentcore_runtime.arn
}
