################################################################################
# ECR Repositories
################################################################################

resource "aws_ecr_repository" "repos" {
  for_each = var.repo_names

  name                 = "${var.prefix}/${each.key}"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  tags = merge(var.tags, {
    Name = "${var.prefix}-${each.key}"
  })
}

resource "aws_ecr_lifecycle_policy" "repos" {
  for_each = aws_ecr_repository.repos

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last ${var.max_image_count} images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.max_image_count
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
