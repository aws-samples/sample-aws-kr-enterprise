"""WebStack — S3 + CloudFront demo UI hosting with OAC."""

import os

import aws_cdk as cdk
import aws_cdk.aws_cloudfront as cloudfront
import aws_cdk.aws_cloudfront_origins as origins
import aws_cdk.aws_s3 as s3
import aws_cdk.aws_s3_deployment as s3deploy
from constructs import Construct


class WebStack(cdk.NestedStack):
    """S3 bucket + CloudFront distribution for the demo web UI."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        api_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        self._api_url = api_url

        # --- S3 Bucket (private, block all public access) ---
        self.bucket = s3.Bucket(
            self,
            "DemoUiBucket",
            bucket_name=None,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # --- CloudFront Distribution with OAC ---
        self.distribution = cloudfront.Distribution(
            self,
            "DemoUiDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    self.bucket,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=cdk.Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=cdk.Duration.seconds(0),
                ),
            ],
        )

        # --- Deploy web assets from web/dist/ if it exists ---
        web_dist_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "web",
            "dist",
        )

        if os.path.isdir(web_dist_path):
            s3deploy.BucketDeployment(
                self,
                "DeployWebAssets",
                sources=[s3deploy.Source.asset(web_dist_path)],
                destination_bucket=self.bucket,
                distribution=self.distribution,
                distribution_paths=["/index.html"],
            )

        # --- Outputs ---
        self.demo_url = f"https://{self.distribution.distribution_domain_name}"

        cdk.CfnOutput(
            self,
            "DemoUrl",
            value=self.demo_url,
            description="CloudFront URL for the demo web UI",
        )

        cdk.CfnOutput(
            self,
            "BucketName",
            value=self.bucket.bucket_name,
            description="S3 bucket for demo UI assets",
        )
