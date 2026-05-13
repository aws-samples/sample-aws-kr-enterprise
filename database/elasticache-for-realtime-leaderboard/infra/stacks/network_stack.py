"""NetworkStack — VPC, subnets, security groups, and VPC endpoints."""

import aws_cdk as cdk
import aws_cdk.aws_ec2 as ec2
from constructs import Construct


class NetworkStack(cdk.NestedStack):
    """Creates the VPC with private isolated subnets and VPC endpoints for AWS services."""

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # VPC with PRIVATE_ISOLATED subnets only (no NAT gateway cost).
        # Lambda accesses AWS services via VPC endpoints.
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name="leaderboard-vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )

        # Security group for Lambda functions
        self.lambda_sg = ec2.SecurityGroup(
            self,
            "LambdaSg",
            vpc=self.vpc,
            security_group_name="leaderboard-lambda-sg",
            description="Security group for leaderboard Lambda functions",
            allow_all_outbound=True,
        )

        # Security group for Valkey (ElastiCache)
        self.valkey_sg = ec2.SecurityGroup(
            self,
            "ValkeySg",
            vpc=self.vpc,
            security_group_name="leaderboard-valkey-sg",
            description="Security group for ElastiCache Valkey cluster",
            allow_all_outbound=False,
        )

        # Allow inbound to Valkey only from Lambda SG on port 6379
        self.valkey_sg.add_ingress_rule(
            peer=self.lambda_sg,
            connection=ec2.Port.tcp(6379),
            description="Allow Lambda to connect to Valkey",
        )

        # VPC Gateway Endpoints (free)
        self.vpc.add_gateway_endpoint(
            "DynamoDbEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
        )

        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        # VPC Interface Endpoints (for Lambda in isolated subnets)
        private_subnets = ec2.SubnetSelection(
            subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
        )

        self.vpc.add_interface_endpoint(
            "SecretsManagerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            subnets=private_subnets,
        )

        self.vpc.add_interface_endpoint(
            "SqsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SQS,
            subnets=private_subnets,
        )

        self.vpc.add_interface_endpoint(
            "CloudWatchLogsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            subnets=private_subnets,
        )

        self.vpc.add_interface_endpoint(
            "CloudWatchMonitoringEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_MONITORING,
            subnets=private_subnets,
        )
