################################################################################
# KMS CMK for DynamoDB encryption (CKV_AWS_119)
################################################################################

resource "aws_kms_key" "dynamodb" {
  description             = "${var.prefix} DynamoDB table encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = merge(var.tags, {
    Name = "${var.prefix}-dynamodb-cmk"
  })
}

resource "aws_kms_alias" "dynamodb" {
  name          = "alias/${var.prefix}-dynamodb"
  target_key_id = aws_kms_key.dynamodb.key_id
}
