import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as lambdaEvents from "aws-cdk-lib/aws-lambda-event-sources";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as iam from "aws-cdk-lib/aws-iam";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as agentcore from "aws-cdk-lib/aws-bedrockagentcore";
import { Construct } from "constructs";
import { FIX_BRANCH_PREFIX, WORKLOAD_PIPELINE, WORKLOAD_REPOSITORY } from "./gitops-stack";

const KIRO_MODEL = "claude-opus-5.5";

/** SHA-256 over the kiro-agent-sdk sources the agent image installs (pyproject.toml, src/, the prewarm script). */
export function sdkFingerprint(sdkPath: string): string {
  const files: string[] = ["pyproject.toml", "scripts/prewarm_kiro.py"];
  const walk = (relative: string) => {
    for (const entry of fs.readdirSync(path.join(sdkPath, relative), { withFileTypes: true })) {
      const child = path.posix.join(relative, entry.name);
      // Source files only: caches, build metadata (*.egg-info) and dot directories differ between checkouts.
      if (entry.name.startsWith(".") || entry.name === "__pycache__" || entry.name.endsWith(".egg-info")) continue;
      if (entry.isDirectory()) walk(child);
      else if (entry.isFile() && (entry.name.endsWith(".py") || entry.name === "py.typed")) files.push(child);
    }
  };
  walk("src");
  const hash = crypto.createHash("sha256");
  for (const file of files.sort()) {
    hash.update(file + "\0").update(fs.readFileSync(path.join(sdkPath, file))).update("\0");
  }
  return hash.digest("hex");
}

export class IncidentToPrPlatformStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ========== DynamoDB ==========
    const workloadsTable = new dynamodb.Table(this, "WorkloadsTable", {
      tableName: "incident-to-pr-workloads",
      partitionKey: { name: "workload_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST, removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const reportsTable = new dynamodb.Table(this, "ReportsTable", {
      tableName: "incident-to-pr-reports",
      partitionKey: { name: "report_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST, removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    reportsTable.addGlobalSecondaryIndex({
      indexName: "workload-created-index",
      partitionKey: { name: "workload_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "created_at", type: dynamodb.AttributeType.STRING },
    });

    // ========== S3 ==========
    const knowledgeBucket = new s3.Bucket(this, "KnowledgeBucket", {
      bucketName: `incident-to-pr-knowledge-${this.account}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY, autoDeleteObjects: true, enforceSSL: true,
      // Uploads use presigned POST, whose policy fixes the key and the size range.
      cors: [{ allowedMethods: [s3.HttpMethods.POST], allowedOrigins: ["*"], allowedHeaders: ["*"] }],
    });
    new s3deploy.BucketDeployment(this, "SeedKnowledge", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "../../knowledge-base"))],
      destinationBucket: knowledgeBucket,
    });

    const buildBucket = new s3.Bucket(this, "BuildBucket", {
      bucketName: `incident-to-pr-build-${this.account}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY, autoDeleteObjects: true,
    });

    const webBucket = new s3.Bucket(this, "WebBucket", {
      bucketName: `incident-to-pr-web-${this.account}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY, autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    // ========== Chat memory (replaces AgentCore Memory) ==========
    const memoryTable = new dynamodb.Table(this, "MemoryTable", {
      tableName: "incident-to-pr-chat-memory",
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: "expires_at",
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST, removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ========== Kiro authentication ==========
    // apikey (default): a Kiro API key stored by the user in Secrets Manager before deployment.
    // web: a Kiro web login token kept fresh by the refresher Lambda (see scripts/kiro_web_login.py).
    const authMode = this.node.tryGetContext("kiroAuthMode") ?? "apikey";
    if (authMode !== "apikey" && authMode !== "web") throw new Error("kiroAuthMode must be apikey or web");
    const kiroSecretName = authMode === "web" ? "incident-to-pr/kiro-web-token" : "incident-to-pr/kiro-api-key";
    const kiroSecret = secretsmanager.Secret.fromSecretNameV2(this, "KiroSecret", kiroSecretName);
    // Web UI password: {"password", "signing_key"}, created by the user before deployment (see README).
    const webPasswordSecretName = "incident-to-pr/web-password";
    const webPasswordSecret = secretsmanager.Secret.fromSecretNameV2(this, "WebPassword", webPasswordSecretName);
    if (authMode === "web") {
      const loginSecret = secretsmanager.Secret.fromSecretNameV2(this, "KiroWebLogin", "incident-to-pr/kiro-web-login");
      const refresher = new lambda.Function(this, "KiroTokenRefresher", {
        functionName: "incident-to-pr-kiro-token-refresher", runtime: lambda.Runtime.PYTHON_3_12, handler: "handler.handler",
        code: lambda.Code.fromAsset(path.join(__dirname, "../../src/lambdas/kiro_token_refresher")),
        timeout: cdk.Duration.seconds(60), memorySize: 256,
        environment: { LOGIN_SECRET_ID: "incident-to-pr/kiro-web-login", TOKEN_SECRET_ID: kiroSecretName },
      });
      refresher.addToRolePolicy(new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
        resources: [`${loginSecret.secretArn}-*`, `${kiroSecret.secretArn}-*`],
      }));
      new events.Rule(this, "KiroTokenRefreshSchedule", {
        ruleName: "incident-to-pr-kiro-token-refresh", schedule: events.Schedule.rate(cdk.Duration.minutes(15)),
        targets: [new targets.LambdaFunction(refresher)],
      });
    }

    // ========== AgentCore Runtime (container via CodeBuild) ==========
    const workloadRepositoryArn = `arn:aws:codecommit:${this.region}:${this.account}:${WORKLOAD_REPOSITORY}`;
    const workloadPipelineArn = `arn:aws:codepipeline:${this.region}:${this.account}:${WORKLOAD_PIPELINE}`;
    const runtimeLogs = `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`;
    const agentCoreRole = new iam.Role(this, "AgentCoreRole", {
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      inlinePolicies: { perms: new iam.PolicyDocument({ statements: [
        // Read-only: investigation (Describe and query APIs, most without resource-level permissions)
        new iam.PolicyStatement({
          actions: ["logs:StartQuery", "logs:GetQueryResults", "logs:StopQuery", "logs:DescribeLogGroups",
            "cloudwatch:Describe*", "cloudwatch:Get*", "cloudwatch:List*",
            "cloudtrail:LookupEvents",
            "config:Get*", "config:List*", "config:Describe*",
            "ec2:Describe*", "ec2:GetConsoleOutput",
            "elasticloadbalancing:Describe*", "rds:Describe*", "autoscaling:Describe*",
            "ecs:Describe*", "ecs:List*",
            "lambda:GetFunction", "lambda:ListFunctions"],
          resources: ["*"],
        }),
        // The runtime's own logs
        new iam.PolicyStatement({
          actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
          resources: [runtimeLogs, `${runtimeLogs}:log-stream:*`],
        }),
        // Demo data: knowledge documents, reports and chat memory
        new iam.PolicyStatement({ actions: ["s3:GetObject", "s3:ListBucket"],
          resources: [knowledgeBucket.bucketArn, knowledgeBucket.arnForObjects("*")] }),
        new iam.PolicyStatement({
          actions: ["dynamodb:GetItem", "dynamodb:Scan", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"],
          resources: [reportsTable.tableArn, memoryTable.tableArn],
        }),
        new iam.PolicyStatement({ actions: ["ecr:GetAuthorizationToken"], resources: ["*"] }),
        new iam.PolicyStatement({ actions: ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
          resources: [`arn:aws:ecr:${this.region}:${this.account}:repository/incident-to-pr-agent`] }),
        // Workload repository and pipeline: read everything; agents never change AWS resources directly.
        new iam.PolicyStatement({
          actions: ["codecommit:GetRepository", "codecommit:GetBranch", "codecommit:GetCommit",
            "codecommit:GetDifferences", "codecommit:GetFile", "codecommit:GetFolder", "codecommit:GetBlob",
            "codecommit:GetPullRequest", "codecommit:ListPullRequests"],
          resources: [workloadRepositoryArn],
        }),
        new iam.PolicyStatement({ actions: ["codepipeline:ListPipelineExecutions", "codepipeline:GetPipelineState"],
          resources: [workloadPipelineArn] }),
        // Write: new fix branches, file changes on them, and pull requests into main. Merging stays with people.
        // PutFile is the documented action for the codecommit:References key, so changes are made one file at a time.
        new iam.PolicyStatement({ actions: ["codecommit:CreateBranch", "codecommit:CreatePullRequest"],
          resources: [workloadRepositoryArn] }),
        new iam.PolicyStatement({
          actions: ["codecommit:PutFile"], resources: [workloadRepositoryArn],
          conditions: { StringLike: { "codecommit:References": [`refs/heads/${FIX_BRANCH_PREFIX}*`] } },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.DENY,
          actions: ["codecommit:Merge*", "codecommit:GitPush", "codecommit:CreateCommit", "codecommit:DeleteFile",
            "codecommit:DeleteBranch", "codecommit:UpdatePullRequestApprovalState",
            "codecommit:OverridePullRequestApprovalRules", "codecommit:UpdateDefaultBranch"],
          resources: [workloadRepositoryArn],
        }),
        // Kiro credentials
        new iam.PolicyStatement({ actions: ["secretsmanager:GetSecretValue"], resources: [`${kiroSecret.secretArn}-*`] }),
      ]})},
    });

    // ECR repository
    const ecr = new cdk.aws_ecr.Repository(this, "AgentEcr", {
      repositoryName: "incident-to-pr-agent",
      removalPolicy: cdk.RemovalPolicy.DESTROY, emptyOnDelete: true,
    });

    // CodeBuild project to build + push Docker image
    const buildProject = new cdk.aws_codebuild.Project(this, "AgentBuild", {
      projectName: "incident-to-pr-agent-build",
      source: cdk.aws_codebuild.Source.s3({ bucket: buildBucket, path: "agent-build/" }),
      environment: {
        buildImage: cdk.aws_codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
        privileged: true,
      },
      environmentVariables: {
        ECR_REPO: { value: ecr.repositoryUri },
        AWS_ACCOUNT_ID: { value: this.account },
      },
      buildSpec: cdk.aws_codebuild.BuildSpec.fromObject({
        version: "0.2",
        phases: {
          pre_build: { commands: [
            "aws ecr get-login-password | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com",
          ]},
          build: { commands: [
            "docker build --no-cache -f app/Dockerfile -t $ECR_REPO:latest .",
            "docker push $ECR_REPO:latest",
          ]},
        },
      }),
    });
    ecr.grantPullPush(buildProject);
    buildBucket.grantRead(buildProject);

    // Upload agent source and the kiro-agent-sdk source to S3 for CodeBuild
    const sdkPath = path.resolve(__dirname, "..", this.node.tryGetContext("kiroAgentSdkPath") ?? "../../kiro-agent-sdk");
    // The demo is tested with one kiro-agent-sdk; refuse to build another one unless asked to.
    const expectedSdk = this.node.tryGetContext("kiroAgentSdkSha256");
    const actualSdk = sdkFingerprint(sdkPath);
    if (expectedSdk && actualSdk !== expectedSdk && this.node.tryGetContext("allowUntestedSdk") !== "true") {
      throw new Error(`kiro-agent-sdk at ${sdkPath} has fingerprint ${actualSdk}, but this demo was tested with ` +
        `${expectedSdk} (cdk.json kiroAgentSdkSha256). Use the tested SDK or deploy with -c allowUntestedSdk=true.`);
    }
    const agentSourceDeploy = new s3deploy.BucketDeployment(this, "DeployAgentSource", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "../../src/agents/app"), { exclude: ["**/__pycache__", "*.egg-info", "build"] })],
      destinationBucket: buildBucket, destinationKeyPrefix: "agent-build/app",
    });
    const sdkSourceDeploy = new s3deploy.BucketDeployment(this, "DeployKiroAgentSdkSource", {
      sources: [s3deploy.Source.asset(sdkPath, {
        exclude: ["*", "!pyproject.toml", "!README.md", "!LICENSE", "!src", "!src/**",
                  "!scripts", "!scripts/prewarm_kiro.py", "**/__pycache__"],
      })],
      destinationBucket: buildBucket, destinationKeyPrefix: "agent-build/kiro-agent-sdk",
    });

    // Custom Resource Lambda: builds Docker image and optionally updates AgentCore Runtime
    const triggerBuildFn = new lambda.Function(this, "TriggerBuildFn", {
      runtime: lambda.Runtime.PYTHON_3_12, handler: "index.handler", timeout: cdk.Duration.seconds(600), memorySize: 256,
      code: lambda.Code.fromInline(`
import boto3, json, time, urllib.request
cb = boto3.client("codebuild")
ac = boto3.client("bedrock-agentcore-control")
def handler(event, context):
    pid = "agent-build"
    try:
        if event["RequestType"] in ("Create", "Update"):
            props = event["ResourceProperties"]
            proj = props["ProjectName"]
            if props.get("SkipBuild") != "true":
                r = cb.start_build(projectName=proj)
                bid = r["build"]["id"]
                print(f"Build started: {bid}")
                for _ in range(60):
                    time.sleep(10)
                    b = cb.batch_get_builds(ids=[bid])["builds"][0]
                    s = b["buildStatus"]
                    print(f"Status: {s}")
                    if s == "SUCCEEDED": break
                    if s in ("FAILED","FAULT","STOPPED","TIMED_OUT"):
                        raise Exception(f"Build {s}")
                else:
                    raise Exception("Build timeout")
            runtime_id = props.get("RuntimeId", "")
            if runtime_id:
                env_vars = json.loads(props.get("EnvVars", "{}"))
                print(f"Updating runtime {runtime_id}")
                ac.update_agent_runtime(
                    agentRuntimeId=runtime_id,
                    agentRuntimeArtifact={"containerConfiguration":{"containerUri":props["ContainerUri"]}},
                    roleArn=props["RoleArn"],
                    networkConfiguration={"networkMode":"PUBLIC"},
                    environmentVariables=env_vars,
                )
                for _ in range(30):
                    time.sleep(10)
                    st = ac.get_agent_runtime(agentRuntimeId=runtime_id)["status"]
                    print(f"Runtime: {st}")
                    if st == "READY": break
        _send(event, "SUCCESS", pid)
    except Exception as e:
        print(e)
        _send(event, "FAILED", pid, str(e))
def _send(event, status, pid, reason="OK"):
    body = json.dumps({"Status":status,"Reason":reason,"PhysicalResourceId":pid,"StackId":event["StackId"],"RequestId":event["RequestId"],"LogicalResourceId":event["LogicalResourceId"],"Data":{}})
    urllib.request.urlopen(urllib.request.Request(event["ResponseURL"],data=body.encode(),method="PUT",headers={"Content-Type":"","Content-Length":str(len(body))}),timeout=10)
`),
    });
    triggerBuildFn.addToRolePolicy(new iam.PolicyStatement({ actions: ["codebuild:StartBuild", "codebuild:BatchGetBuilds"], resources: [buildProject.projectArn] }));
    triggerBuildFn.addToRolePolicy(new iam.PolicyStatement({ actions: ["bedrock-agentcore:UpdateAgentRuntime", "bedrock-agentcore:GetAgentRuntime"],
      resources: [`arn:aws:bedrock-agentcore:${this.region}:${this.account}:runtime/incident_to_pr_agent-*`] }));
    triggerBuildFn.addToRolePolicy(new iam.PolicyStatement({ actions: ["iam:PassRole"], resources: [agentCoreRole.roleArn] }));

    // Step 1: Build image BEFORE Runtime creation (so ECR has :latest)
    const buildImage = new cdk.CustomResource(this, "BuildImage", {
      serviceToken: triggerBuildFn.functionArn,
      properties: { ProjectName: buildProject.projectName, Version: Date.now().toString() },
    });
    buildImage.node.addDependency(buildProject);
    buildImage.node.addDependency(agentSourceDeploy);
    buildImage.node.addDependency(sdkSourceDeploy);

    const runtimeEnvVars = {
      KIRO_MODEL: KIRO_MODEL, KIRO_AUTH_MODE: authMode, KIRO_SECRET_ID: kiroSecretName,
      KNOWLEDGE_BUCKET: knowledgeBucket.bucketName,
      AWS_REGION: this.region, AWS_DEFAULT_REGION: this.region,
      REPORTS_TABLE: reportsTable.tableName, WORKLOADS_TABLE: workloadsTable.tableName,
      MEMORY_TABLE: memoryTable.tableName,
      WORKLOAD_REPOSITORY: WORKLOAD_REPOSITORY, WORKLOAD_PIPELINE: WORKLOAD_PIPELINE,
      FIX_BRANCH_PREFIX: FIX_BRANCH_PREFIX,
    };

    const runtime = new agentcore.CfnRuntime(this, "AgentRuntime", {
      agentRuntimeName: "incident_to_pr_agent", roleArn: agentCoreRole.roleArn,
      networkConfiguration: { networkMode: "PUBLIC" },
      agentRuntimeArtifact: {
        containerConfiguration: { containerUri: `${ecr.repositoryUri}:latest` },
      },
      environmentVariables: runtimeEnvVars,
    });
    runtime.node.addDependency(ecr);
    runtime.node.addDependency(buildImage);  // ECR image must exist before Runtime creation

    // The Lambdas invoke the DEFAULT endpoint, which AgentCore creates with the runtime and points at its latest version.

    // ========== Lambda: Orchestrator ==========
    const orchestrator = new lambda.Function(this, "Orchestrator", {
      functionName: "incident-to-pr-orchestrator", runtime: lambda.Runtime.PYTHON_3_12, handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../src/lambdas/orchestrator")),
      timeout: cdk.Duration.seconds(30), memorySize: 256, reservedConcurrentExecutions: 5,
      environment: { REPORTS_TABLE: reportsTable.tableName, WORKLOADS_TABLE: workloadsTable.tableName, AGENT_RUNTIME_ARN: runtime.attrAgentRuntimeArn },
    });
    reportsTable.grantWriteData(orchestrator); workloadsTable.grantReadData(orchestrator);
    const runtimeInvokeResources = [runtime.attrAgentRuntimeArn, `${runtime.attrAgentRuntimeArn}/*`];
    orchestrator.addToRolePolicy(new iam.PolicyStatement({ actions: ["bedrock-agentcore:InvokeAgentRuntime"], resources: runtimeInvokeResources }));

    // ========== Lambda: API Handler ==========
    // The REST and WebSocket handlers share web_auth.py, so their code is src/lambdas with only their own folder.
    const lambdaCode = (folder: string) => lambda.Code.fromAsset(path.join(__dirname, "../../src/lambdas"), {
      exclude: ["*", "!web_auth.py", `!${folder}`, `!${folder}/**`, "**/__pycache__"],
    });
    const apiHandler = new lambda.Function(this, "ApiHandler", {
      functionName: "incident-to-pr-api-handler", runtime: lambda.Runtime.PYTHON_3_12, handler: "api_handler.handler.handler",
      code: lambdaCode("api_handler"),
      timeout: cdk.Duration.seconds(120), memorySize: 256,
      environment: { REPORTS_TABLE: reportsTable.tableName, WORKLOADS_TABLE: workloadsTable.tableName, KNOWLEDGE_BUCKET: knowledgeBucket.bucketName, AGENT_RUNTIME_ARN: runtime.attrAgentRuntimeArn, WEB_PASSWORD_SECRET_ID: webPasswordSecretName, WORKLOAD_REPOSITORY: WORKLOAD_REPOSITORY, WORKLOAD_PIPELINE: WORKLOAD_PIPELINE },
    });
    // Report status follows the fix pull request: open, merged, then deployed by the workload pipeline.
    apiHandler.addToRolePolicy(new iam.PolicyStatement({ actions: ["codecommit:GetPullRequest", "codecommit:GetCommit"], resources: [workloadRepositoryArn] }));
    apiHandler.addToRolePolicy(new iam.PolicyStatement({ actions: ["codepipeline:ListPipelineExecutions"], resources: [workloadPipelineArn] }));
    reportsTable.grantReadWriteData(apiHandler); workloadsTable.grantReadWriteData(apiHandler);
    knowledgeBucket.grantReadWrite(apiHandler);
    apiHandler.addToRolePolicy(new iam.PolicyStatement({ actions: ["cloudwatch:DescribeAlarms"], resources: ["*"] }));
    apiHandler.addToRolePolicy(new iam.PolicyStatement({ actions: ["bedrock-agentcore:InvokeAgentRuntime"], resources: runtimeInvokeResources }));
    const readWebPassword = new iam.PolicyStatement({ actions: ["secretsmanager:GetSecretValue"], resources: [`${webPasswordSecret.secretArn}-*`] });
    apiHandler.addToRolePolicy(readWebPassword);

    // ========== API Gateway ==========
    const api = new apigateway.RestApi(this, "Api", {
      restApiName: "incident-to-pr-api",
      defaultCorsPreflightOptions: { allowOrigins: apigateway.Cors.ALL_ORIGINS, allowMethods: apigateway.Cors.ALL_METHODS, allowHeaders: ["Content-Type", "Authorization"] },
      // Stage throttling limits bursts; it is not a usage cap. Password guesses get a lower limit.
      deployOptions: {
        throttlingRateLimit: 10, throttlingBurstLimit: 20,
        methodOptions: { "/api/login/POST": { throttlingRateLimit: 1, throttlingBurstLimit: 3 } },
      },
    });
    const integ = new apigateway.LambdaIntegration(apiHandler);
    const apiRes = api.root.addResource("api");
    apiRes.addResource("login").addMethod("POST", integ);
    const workloads = apiRes.addResource("workloads"); workloads.addMethod("GET", integ); workloads.addMethod("POST", integ);
    const wById = workloads.addResource("{workload_id}"); wById.addMethod("GET", integ); wById.addMethod("PUT", integ); wById.addMethod("DELETE", integ);
    wById.addResource("upload-url").addMethod("POST", integ);
    const wDocs = wById.addResource("documents"); wDocs.addMethod("GET", integ); wDocs.addMethod("DELETE", integ);
    wById.addResource("sync").addMethod("POST", integ);
    const reports = apiRes.addResource("reports"); reports.addMethod("GET", integ);
    const rById = reports.addResource("{id}"); rById.addMethod("GET", integ); rById.addMethod("DELETE", integ);
    apiRes.addResource("status").addMethod("GET", integ);
    apiRes.addResource("chat").addMethod("POST", integ);

    // ========== WebSocket API for Chat (answers up to the 15-minute Lambda limit, past REST's 29 seconds) ==========
    const wsHandler = new lambda.Function(this, "WsHandler", {
      functionName: "incident-to-pr-ws-handler",
      runtime: lambda.Runtime.PYTHON_3_12, handler: "ws_handler.handler.handler",
      code: lambdaCode("ws_handler"),
      timeout: cdk.Duration.seconds(900), memorySize: 256,
      environment: {
        AGENT_RUNTIME_ARN: runtime.attrAgentRuntimeArn,
        WS_ENDPOINT: "",  // Updated below after wsApi creation
        WEB_PASSWORD_SECRET_ID: webPasswordSecretName,
      },
    });
    wsHandler.addToRolePolicy(new iam.PolicyStatement({ actions: ["bedrock-agentcore:InvokeAgentRuntime"], resources: runtimeInvokeResources }));
    wsHandler.addToRolePolicy(readWebPassword);

    const wsApi = new cdk.aws_apigatewayv2.CfnApi(this, "WsApi", {
      name: "incident-to-pr-ws",
      protocolType: "WEBSOCKET",
      routeSelectionExpression: "$request.body.action",
    });

    const wsIntegration = new cdk.aws_apigatewayv2.CfnIntegration(this, "WsInteg", {
      apiId: wsApi.ref,
      integrationType: "AWS_PROXY",
      integrationUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${wsHandler.functionArn}/invocations`,
    });

    for (const route of ["$connect", "$disconnect", "sendMessage"]) {
      new cdk.aws_apigatewayv2.CfnRoute(this, `WsRoute${route.replace("$", "")}`, {
        apiId: wsApi.ref,
        routeKey: route,
        target: `integrations/${wsIntegration.ref}`,
      });
    }

    const wsStage = new cdk.aws_apigatewayv2.CfnStage(this, "WsStage", {
      apiId: wsApi.ref,
      stageName: "prod",
      autoDeploy: true,
      defaultRouteSettings: { throttlingRateLimit: 2, throttlingBurstLimit: 5 },
    });
    wsHandler.addToRolePolicy(new iam.PolicyStatement({
      actions: ["execute-api:ManageConnections"],
      resources: [`arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/prod/*/@connections/*`],
    }));

    // Grant API Gateway permission to invoke Lambda
    wsHandler.addPermission("WsApiInvoke", {
      principal: new iam.ServicePrincipal("apigateway.amazonaws.com"),
      sourceArn: `arn:aws:execute-api:${this.region}:${this.account}:${wsApi.ref}/*`,
    });

    // Set WS env vars directly (no Custom Resource needed)
    const wsUrl = `wss://${wsApi.ref}.execute-api.${this.region}.amazonaws.com/prod`;
    const wsEndpoint = `https://${wsApi.ref}.execute-api.${this.region}.amazonaws.com/prod`;
    wsHandler.addEnvironment("WS_ENDPOINT", wsEndpoint);
    apiHandler.addEnvironment("WS_URL", wsUrl);

    // ========== SQS Queue for alarm events ==========
    const alarmQueue = new sqs.Queue(this, "AlarmQueue", {
      queueName: "incident-to-pr-alarm-queue",
      visibilityTimeout: cdk.Duration.seconds(60), // > Lambda timeout (30s)
      retentionPeriod: cdk.Duration.days(1),
    });

    // EventBridge → SQS
    new events.Rule(this, "AlarmRule", {
      ruleName: "incident-to-pr-alarm-trigger",
      eventPattern: { source: ["aws.cloudwatch"], detailType: ["CloudWatch Alarm State Change"], detail: { state: { value: ["ALARM"] } } },
      targets: [new targets.SqsQueue(alarmQueue)],
    });

    // SQS → Lambda (1 at a time)
    orchestrator.addEventSource(new lambdaEvents.SqsEventSource(alarmQueue, { batchSize: 1, maxConcurrency: 5 }));
    orchestrator.addToRolePolicy(new iam.PolicyStatement({ actions: ["sqs:*"], resources: [alarmQueue.queueArn] }));

    // ========== CloudFront ==========
    // The SPA talks only to this origin (/api/*), the chat WebSocket API and the knowledge bucket (upload POST).
    // Markdown from agents cannot load external images or scripts.
    const webHeaders = new cloudfront.ResponseHeadersPolicy(this, "WebHeaders", {
      securityHeadersBehavior: {
        contentSecurityPolicy: { override: true, contentSecurityPolicy: [
          "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'", "img-src 'self' data:",
          "font-src 'self' data:", "object-src 'none'", "base-uri 'none'", "form-action 'self'", "frame-ancestors 'none'",
          `connect-src 'self' wss://${wsApi.ref}.execute-api.${this.region}.amazonaws.com ` +
            `https://${knowledgeBucket.bucketName}.s3.${this.region}.amazonaws.com`,
        ].join("; ") },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override: true },
        strictTransportSecurity: { accessControlMaxAge: cdk.Duration.days(365), includeSubdomains: true, override: true },
      },
    });
    const dist = new cloudfront.Distribution(this, "WebDist", {
      defaultBehavior: { origin: origins.S3BucketOrigin.withOriginAccessControl(webBucket), viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy: webHeaders },
      additionalBehaviors: {
        "/api/*": {
        origin: new origins.HttpOrigin(`${api.restApiId}.execute-api.${this.region}.amazonaws.com`, { originPath: `/${api.deploymentStage.stageName}` }),
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL, viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED, originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
      }},
      defaultRootObject: "index.html",
      errorResponses: [{ httpStatus: 404, responseHttpStatus: 200, responsePagePath: "/index.html" }, { httpStatus: 403, responseHttpStatus: 200, responsePagePath: "/index.html" }],
    });

    new cdk.CfnOutput(this, "WsUrl", { value: `wss://${wsApi.ref}.execute-api.${this.region}.amazonaws.com/prod` });
    new cdk.CfnOutput(this, "ApiUrl", { value: api.url });
    new cdk.CfnOutput(this, "WebUrl", { value: `https://${dist.distributionDomainName}` });

    // ========== Deploy Web UI to S3 + CloudFront Invalidation ==========
    new s3deploy.BucketDeployment(this, "DeployWebUI", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "../../web-ui"), {
        bundling: {
          image: cdk.DockerImage.fromRegistry("node:20-alpine"),
          command: ["sh", "-c", "npm ci && npm run build && cp -r build/* /asset-output/"],
          local: {
            tryBundle(outputDir: string) {
              require("child_process").execSync(
                "npm ci && npm run build",
                { cwd: path.join(__dirname, "../../web-ui"), stdio: "inherit" }
              );
              require("child_process").execSync(
                `cp -r ${path.join(__dirname, "../../web-ui/build")}/* ${outputDir}/`
              );
              return true;
            },
          },
        },
      })],
      destinationBucket: webBucket,
      distribution: dist,
      distributionPaths: ["/*"],
    });

    // Step 2: Update Runtime env vars (after dist for WEB_URL, skips rebuild)
    const fullEnvVars = { ...runtimeEnvVars, WEB_URL: `https://${dist.distributionDomainName}` };
    const buildTrigger = new cdk.CustomResource(this, "TriggerAgentBuild", {
      serviceToken: triggerBuildFn.functionArn,
      properties: {
        ProjectName: buildProject.projectName, Version: Date.now().toString(),
        RuntimeId: runtime.attrAgentRuntimeId,
        ContainerUri: `${ecr.repositoryUri}:latest`,
        RoleArn: agentCoreRole.roleArn,
        EnvVars: JSON.stringify(fullEnvVars),
        SkipBuild: "true",
      },
    });
    buildTrigger.node.addDependency(runtime);
    new cdk.CfnOutput(this, "KnowledgeBucketName", { value: knowledgeBucket.bucketName });
    new cdk.CfnOutput(this, "AgentRuntimeArn", { value: runtime.attrAgentRuntimeArn });
  }
}
