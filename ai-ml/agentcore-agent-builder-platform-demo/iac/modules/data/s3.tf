################################################################################
# S3 — Reports Bucket
################################################################################

resource "aws_s3_bucket" "reports" {
  bucket        = "${var.prefix}-reports"
  force_destroy = true

  tags = merge(var.tags, {
    Name = "${var.prefix}-reports"
  })
}

resource "aws_s3_bucket_public_access_block" "reports" {
  bucket = aws_s3_bucket.reports.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
