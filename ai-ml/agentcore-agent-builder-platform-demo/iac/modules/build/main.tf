################################################################################
# S3 Source Bucket
################################################################################

resource "aws_s3_bucket" "codebuild_source" {
  bucket        = "${var.prefix}-codebuild-source-${var.account_id}-${var.aws_region}"
  force_destroy = true

  tags = merge(var.tags, {
    Name = "${var.prefix}-codebuild-source"
  })
}

resource "aws_s3_bucket_lifecycle_configuration" "codebuild_source" {
  bucket = aws_s3_bucket.codebuild_source.id

  rule {
    id     = "expire-source-archives"
    status = "Enabled"
    filter {}
    expiration {
      days = 7
    }
  }
}

################################################################################
# CodeBuild Projects
################################################################################

resource "aws_codebuild_project" "x86" {
  name         = "${var.prefix}-build-x86"
  description  = "Build amd64 container images (platform-api, frontend)"
  service_role = aws_iam_role.codebuild.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_MEDIUM"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true

    environment_variable {
      name  = "AWS_ACCOUNT_ID"
      value = var.account_id
    }
    environment_variable {
      name  = "AWS_DEFAULT_REGION"
      value = var.aws_region
    }
    environment_variable {
      name  = "IMAGE_PREFIX"
      value = "${var.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.prefix}"
    }
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.codebuild_source.id}/source-x86.zip"
    buildspec = "code/buildspec-x86.yml"
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-build-x86"
    Arch = "amd64"
  })
}

resource "aws_codebuild_project" "arm64" {
  name         = "${var.prefix}-build-arm64"
  description  = "Build arm64 container images (base-image, report-image)"
  service_role = aws_iam_role.codebuild.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_MEDIUM"
    image           = "aws/codebuild/amazonlinux2-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = true

    environment_variable {
      name  = "AWS_ACCOUNT_ID"
      value = var.account_id
    }
    environment_variable {
      name  = "AWS_DEFAULT_REGION"
      value = var.aws_region
    }
    environment_variable {
      name  = "IMAGE_PREFIX"
      value = "${var.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.prefix}"
    }
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.codebuild_source.id}/source-arm64.zip"
    buildspec = "code/buildspec-arm64.yml"
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-build-arm64"
    Arch = "arm64"
  })
}
