import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as autoscaling from "aws-cdk-lib/aws-autoscaling";
import * as rds from "aws-cdk-lib/aws-rds";
import * as iam from "aws-cdk-lib/aws-iam";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as fs from "fs";
import * as path from "path";
import { Construct } from "constructs";

/** Sample 3-tier workload (ALB, two EC2 instances, RDS). Its pipeline deploys this stack from the main branch. */
export class SampleAppStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs: 2, natGateways: 1,
      subnetConfiguration: [
        { cidrMask: 24, name: "Public", subnetType: ec2.SubnetType.PUBLIC },
        { cidrMask: 24, name: "Private", subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      ],
    });

    const albSg = new ec2.SecurityGroup(this, "AlbSg", { vpc, allowAllOutbound: true });
    albSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80));

    const ec2Sg = new ec2.SecurityGroup(this, "Ec2Sg", { vpc, allowAllOutbound: true });
    ec2Sg.addIngressRule(albSg, ec2.Port.tcp(80));

    const rdsSg = new ec2.SecurityGroup(this, "RdsSg", { vpc, allowAllOutbound: true });
    rdsSg.addIngressRule(ec2Sg, ec2.Port.tcp(5432));

    const db = new rds.DatabaseInstance(this, "Db", {
      engine: rds.DatabaseInstanceEngine.postgres({ version: rds.PostgresEngineVersion.VER_16 }),
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
      vpc, vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [rdsSg], databaseName: "sampleapp",
      credentials: rds.Credentials.fromGeneratedSecret("sampleapp", { secretName: "incident-to-pr/rds-credentials" }),
      multiAz: false, allocatedStorage: 20, storageEncrypted: true,
      backupRetention: cdk.Duration.days(0), deleteAutomatedBackups: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const ec2Role = new iam.Role(this, "Ec2Role", {
      assumedBy: new iam.ServicePrincipal("ec2.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonSSMManagedInstanceCore"),
        iam.ManagedPolicy.fromAwsManagedPolicyName("CloudWatchAgentServerPolicy"),
      ],
    });
    db.secret!.grantRead(ec2Role);

    // The application is app/app.py in this repository; user data installs it on every instance.
    const appSource = fs.readFileSync(path.join(__dirname, "..", "app", "app.py"), "utf8");
    // Pinned versions from app/requirements.txt, so instances run the dependencies this repository was tested with.
    const requirements = fs.readFileSync(path.join(__dirname, "..", "app", "requirements.txt"), "utf8")
      .split("\n").map((line) => line.trim()).filter((line) => line && !line.startsWith("#")).join(" ");
    const userData = ec2.UserData.forLinux();
    userData.addCommands(
      "yum update -y", "yum install -y python3.11 python3.11-pip amazon-cloudwatch-agent",
      `pip3.11 install ${requirements}`, "mkdir -p /opt/app",
      `cat > /opt/app/app.py << 'EOF'\n${appSource}EOF`,
      `export AWS_REGION=${cdk.Aws.REGION}`,
      `SECRET_JSON=$(aws secretsmanager get-secret-value --secret-id incident-to-pr/rds-credentials --region $AWS_REGION --query SecretString --output text)`,
      `export DB_HOST=$(echo $SECRET_JSON|python3.11 -c "import sys,json;print(json.load(sys.stdin)['host'])")`,
      `export DB_PORT=$(echo $SECRET_JSON|python3.11 -c "import sys,json;print(json.load(sys.stdin)['port'])")`,
      `export DB_USER=$(echo $SECRET_JSON|python3.11 -c "import sys,json;print(json.load(sys.stdin)['username'])")`,
      `export DB_PASSWORD=$(echo $SECRET_JSON|python3.11 -c "import sys,json;print(json.load(sys.stdin)['password'])")`,
      "cd /opt/app && nohup python3.11 -m gunicorn -w 2 -b 0.0.0.0:80 app:app &",
    );

    const lt = new ec2.LaunchTemplate(this, "Lt", {
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
      machineImage: ec2.MachineImage.latestAmazonLinux2023(),
      securityGroup: ec2Sg, role: ec2Role, userData,
    });
    const asg = new autoscaling.AutoScalingGroup(this, "Asg", {
      vpc, vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      launchTemplate: lt, minCapacity: 2, maxCapacity: 2,
    });

    const alb = new elbv2.ApplicationLoadBalancer(this, "Alb", { vpc, internetFacing: true, securityGroup: albSg });
    const tg = alb.addListener("Http", { port: 80 }).addTargets("Tg", {
      port: 80, targets: [asg],
      healthCheck: { path: "/health", interval: cdk.Duration.seconds(30), healthyThresholdCount: 2, unhealthyThresholdCount: 2 },
    });

    // ========== CloudWatch Alarms (incident-to-pr- prefix for EventBridge matching) ==========
    const ALARM_PREFIX = "incident-to-pr-";

    // ALB: Unhealthy targets
    new cloudwatch.Alarm(this, "AlbUnhealthy", {
      alarmName: `${ALARM_PREFIX}alb-unhealthy`,
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApplicationELB", metricName: "UnHealthyHostCount",
        dimensionsMap: { LoadBalancer: alb.loadBalancerFullName, TargetGroup: tg.targetGroupFullName },
        statistic: "Maximum", period: cdk.Duration.minutes(1),
      }),
      threshold: 1, evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    });

    // ALB: 5XX errors
    new cloudwatch.Alarm(this, "Alb5xx", {
      alarmName: `${ALARM_PREFIX}alb-5xx`,
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApplicationELB", metricName: "HTTPCode_Target_5XX_Count",
        dimensionsMap: { LoadBalancer: alb.loadBalancerFullName },
        statistic: "Sum", period: cdk.Duration.minutes(1),
      }),
      threshold: 5, evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ALB: 4XX errors (client errors spike)
    new cloudwatch.Alarm(this, "Alb4xx", {
      alarmName: `${ALARM_PREFIX}alb-4xx`,
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApplicationELB", metricName: "HTTPCode_Target_4XX_Count",
        dimensionsMap: { LoadBalancer: alb.loadBalancerFullName },
        statistic: "Sum", period: cdk.Duration.minutes(5),
      }),
      threshold: 50, evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ALB: High response time
    new cloudwatch.Alarm(this, "AlbLatency", {
      alarmName: `${ALARM_PREFIX}alb-high-latency`,
      metric: new cloudwatch.Metric({
        namespace: "AWS/ApplicationELB", metricName: "TargetResponseTime",
        dimensionsMap: { LoadBalancer: alb.loadBalancerFullName },
        statistic: "Average", period: cdk.Duration.minutes(1),
      }),
      threshold: 5, evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // EC2/ASG: CPU utilization
    new cloudwatch.Alarm(this, "Ec2CpuHigh", {
      alarmName: `${ALARM_PREFIX}ec2-cpu-high`,
      metric: new cloudwatch.Metric({
        namespace: "AWS/EC2", metricName: "CPUUtilization",
        dimensionsMap: { AutoScalingGroupName: asg.autoScalingGroupName },
        statistic: "Average", period: cdk.Duration.minutes(5),
      }),
      threshold: 80, evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    });

    // EC2: Status check failed
    new cloudwatch.Alarm(this, "Ec2StatusCheck", {
      alarmName: `${ALARM_PREFIX}ec2-status-check`,
      metric: new cloudwatch.Metric({
        namespace: "AWS/EC2", metricName: "StatusCheckFailed",
        dimensionsMap: { AutoScalingGroupName: asg.autoScalingGroupName },
        statistic: "Maximum", period: cdk.Duration.minutes(1),
      }),
      threshold: 1, evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    });

    // RDS: CPU utilization
    new cloudwatch.Alarm(this, "RdsCpuHigh", {
      alarmName: `${ALARM_PREFIX}rds-cpu-high`,
      metric: new cloudwatch.Metric({
        namespace: "AWS/RDS", metricName: "CPUUtilization",
        dimensionsMap: { DBInstanceIdentifier: db.instanceIdentifier },
        statistic: "Average", period: cdk.Duration.minutes(5),
      }),
      threshold: 80, evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    });

    // RDS: Free storage space low
    new cloudwatch.Alarm(this, "RdsStorageLow", {
      alarmName: `${ALARM_PREFIX}rds-storage-low`,
      metric: new cloudwatch.Metric({
        namespace: "AWS/RDS", metricName: "FreeStorageSpace",
        dimensionsMap: { DBInstanceIdentifier: db.instanceIdentifier },
        statistic: "Minimum", period: cdk.Duration.minutes(5),
      }),
      threshold: 2 * 1024 * 1024 * 1024, // 2GB
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
    });

    // RDS: Read latency high
    new cloudwatch.Alarm(this, "RdsReadLatency", {
      alarmName: `${ALARM_PREFIX}rds-read-latency`,
      metric: new cloudwatch.Metric({
        namespace: "AWS/RDS", metricName: "ReadLatency",
        dimensionsMap: { DBInstanceIdentifier: db.instanceIdentifier },
        statistic: "Average", period: cdk.Duration.minutes(5),
      }),
      threshold: 0.1, // 100ms
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    });

    // ========== Outputs ==========
    new cdk.CfnOutput(this, "AlbDns", { value: alb.loadBalancerDnsName });
    new cdk.CfnOutput(this, "AlbFullName", { value: alb.loadBalancerFullName });
    new cdk.CfnOutput(this, "TgFullName", { value: tg.targetGroupFullName });
    new cdk.CfnOutput(this, "AsgName", { value: asg.autoScalingGroupName });
    new cdk.CfnOutput(this, "Ec2SgId", { value: ec2Sg.securityGroupId });
    new cdk.CfnOutput(this, "RdsSgId", { value: rdsSg.securityGroupId });
  }
}
