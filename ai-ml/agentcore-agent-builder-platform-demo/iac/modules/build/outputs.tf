output "codebuild_project_arm64" {
  value = aws_codebuild_project.arm64.name
}

output "codebuild_project_x86" {
  value = aws_codebuild_project.x86.name
}

output "source_bucket" {
  value = aws_s3_bucket.codebuild_source.id
}

output "codebuild_role_arn" {
  value = aws_iam_role.codebuild.arn
}
