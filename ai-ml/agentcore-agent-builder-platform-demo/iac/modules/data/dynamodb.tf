################################################################################
# DynamoDB — Platform Table
################################################################################

resource "aws_dynamodb_table" "platform" {
  name         = "${var.prefix}-platform"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "createdAt"
    type = "S"
  }

  attribute {
    name = "createdBy"
    type = "S"
  }

  attribute {
    name = "agentId"
    type = "S"
  }

  attribute {
    name = "entityType"
    type = "S"
  }

  global_secondary_index {
    name            = "status-index"
    hash_key        = "status"
    range_key       = "createdAt"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "owner-index"
    hash_key        = "createdBy"
    range_key       = "agentId"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "entityType-index"
    hash_key        = "entityType"
    range_key       = "PK"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "sk-pk-index"
    hash_key        = "SK"
    range_key       = "PK"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-platform"
  })
}

################################################################################
# DynamoDB — Incidents Table
################################################################################

resource "aws_dynamodb_table" "incidents" {
  name         = "${var.prefix}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  tags = merge(var.tags, {
    Name = "${var.prefix}-incidents"
  })
}
