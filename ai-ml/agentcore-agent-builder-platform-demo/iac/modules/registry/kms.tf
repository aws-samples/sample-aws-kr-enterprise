################################################################################
# KMS CMK for ECR image encryption (CKV_AWS_136)
################################################################################

resource "aws_kms_key" "ecr" {
  description             = "${var.prefix} ECR repository encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = merge(var.tags, {
    Name = "${var.prefix}-ecr-cmk"
  })
}

resource "aws_kms_alias" "ecr" {
  name          = "alias/${var.prefix}-ecr"
  target_key_id = aws_kms_key.ecr.key_id
}
