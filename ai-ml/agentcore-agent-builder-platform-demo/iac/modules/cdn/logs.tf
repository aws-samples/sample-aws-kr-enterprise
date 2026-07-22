################################################################################
# CloudFront access log bucket (CKV_AWS_86)
################################################################################

resource "aws_s3_bucket" "cf_logs" {
  bucket        = "${var.prefix}-cf-logs-${var.account_id}-${var.aws_region}"
  force_destroy = true

  tags = merge(var.tags, {
    Name = "${var.prefix}-cf-logs"
  })
}

resource "aws_s3_bucket_ownership_controls" "cf_logs" {
  bucket = aws_s3_bucket.cf_logs.id
  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_acl" "cf_logs" {
  depends_on = [aws_s3_bucket_ownership_controls.cf_logs]
  bucket     = aws_s3_bucket.cf_logs.id
  acl        = "log-delivery-write"
}
