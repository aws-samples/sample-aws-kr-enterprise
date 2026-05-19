################################################################################
# Route53 — aiops-v2.<domain> → CloudFront
################################################################################

resource "aws_route53_record" "cloudfront" {
  zone_id = var.route53_zone_id
  name    = "aiops-v2.${var.domain_name}"
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.main.domain_name
    zone_id                = aws_cloudfront_distribution.main.hosted_zone_id
    evaluate_target_health = false
  }
}
