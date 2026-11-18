import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as codecommit from "aws-cdk-lib/aws-codecommit";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as codepipeline from "aws-cdk-lib/aws-codepipeline";
import * as actions from "aws-cdk-lib/aws-codepipeline-actions";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3assets from "aws-cdk-lib/aws-s3-assets";
import { Construct } from "constructs";

// The incident analysis platform finds the workload repository and pipeline by these names.
export const WORKLOAD_REPOSITORY = "incident-to-pr-workload";
export const WORKLOAD_PIPELINE = "incident-to-pr-workload";
export const WORKLOAD_STACK = "IncidentToPrSampleAppStack";
// Branches the agents may create; everything else, including main, stays read-only for them.
export const FIX_BRANCH_PREFIX = "incident/";

/**
 * GitOps for the sample workload: a CodeCommit repository holds its IaC and application code, and every change to
 * main is deployed by CodePipeline. Pull requests get a cdk diff comment before anyone merges them.
 */
export class GitOpsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // The first commit is the workload/ directory of this demo (dependencies and build output left out).
    const seed = new s3assets.Asset(this, "WorkloadSeed", {
      path: path.join(__dirname, "../../workload"), exclude: ["node_modules", "cdk.out", "*.js", "*.d.ts"],
    });
    const repository = new codecommit.Repository(this, "WorkloadRepository", {
      repositoryName: WORKLOAD_REPOSITORY,
      description: "Sample workload of the Incident to PR demo on Kiro: IaC and application code, deployed from main",
      code: codecommit.Code.fromAsset(seed, "main"),
    });

    // Only the main deployment runs with the CDK bootstrap roles of this account and Region.
    const assumeCdkRoles = new iam.PolicyStatement({
      actions: ["sts:AssumeRole"],
      resources: [`arn:aws:iam::${this.account}:role/cdk-*-${this.account}-${this.region}`],
    });
    const nodeEnvironment = { buildImage: codebuild.LinuxBuildImage.STANDARD_7_0, computeType: codebuild.ComputeType.SMALL };

    // ========== Deploy: main -> IncidentToPrSampleAppStack ==========
    const deploy = new codebuild.PipelineProject(this, "WorkloadDeploy", {
      projectName: "incident-to-pr-workload-deploy",
      environment: nodeEnvironment,
      timeout: cdk.Duration.minutes(60),
      buildSpec: codebuild.BuildSpec.fromObject({
        version: "0.2",
        phases: {
          install: { "runtime-versions": { nodejs: 20 }, commands: ["npm ci --no-audit --no-fund"] },
          build: { commands: [`npx cdk deploy ${WORKLOAD_STACK} --require-approval never --progress events`] },
        },
      }),
    });
    deploy.addToRolePolicy(assumeCdkRoles);

    const source = new codepipeline.Artifact("Source");
    // The default artifact bucket outlives the stack; this one is deleted with it.
    const artifacts = new s3.Bucket(this, "WorkloadPipelineArtifacts", {
      removalPolicy: cdk.RemovalPolicy.DESTROY, autoDeleteObjects: true, enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, encryption: s3.BucketEncryption.S3_MANAGED,
    });
    new codepipeline.Pipeline(this, "WorkloadPipeline", {
      pipelineName: WORKLOAD_PIPELINE,
      artifactBucket: artifacts,
      pipelineType: codepipeline.PipelineType.V2,
      crossAccountKeys: false,
      stages: [
        { stageName: "Source", actions: [new actions.CodeCommitSourceAction({
          actionName: "main", repository, branch: "main", output: source, trigger: actions.CodeCommitTrigger.EVENTS,
        })] },
        { stageName: "Deploy", actions: [new actions.CodeBuildAction({
          actionName: "cdk-deploy", project: deploy, input: source,
        })] },
      ],
    });

    // ========== Pull request check: cdk diff as a PR comment ==========
    // The check runs code that nobody has reviewed yet (npm and the CDK app of the pull request), so its role can only
    // read the deployed workload template and comment on pull requests: no CDK bootstrap roles, no AWS writes.
    const check = new codebuild.Project(this, "WorkloadPullRequestCheck", {
      projectName: "incident-to-pr-workload-pr-check",
      source: codebuild.Source.codeCommit({ repository }),
      environment: nodeEnvironment,
      timeout: cdk.Duration.minutes(30),
      buildSpec: codebuild.BuildSpec.fromObject({
        version: "0.2",
        // Keeps Node runtime notices out of the comment so it shows only the diff.
        env: { variables: { NODE_NO_WARNINGS: "1" } },
        phases: {
          install: { "runtime-versions": { nodejs: 20 }, commands: ["npm ci --ignore-scripts --no-audit --no-fund"] },
          build: { commands: [
            // A template diff reads the deployed template with this role's own credentials; a change-set diff would need
            // the deploy role. cdk diff exits 0 with or without changes; a synth error is part of its output either way.
            `npx cdk diff ${WORKLOAD_STACK} --method=template --no-color > diff.txt 2>&1 || true`,
            "printf 'cdk diff for %s\\n\\nThe pull request code produced this diff without AWS write access. Review the file changes themselves.\\n\\n```\\n' \"$SOURCE_COMMIT\" > comment.txt",
            // The role cannot assume the bootstrap lookup role, which is the point; that notice is not part of the diff.
            "grep -v -e 'lookup-role' -e 'Lookup role' diff.txt | tail -c 9000 >> comment.txt",
            "printf '\\n```\\n' >> comment.txt",
            `aws codecommit post-comment-for-pull-request --repository-name ${WORKLOAD_REPOSITORY}` +
              " --pull-request-id \"$PULL_REQUEST_ID\" --before-commit-id \"$DESTINATION_COMMIT\"" +
              " --after-commit-id \"$SOURCE_COMMIT\" --content file://comment.txt",
          ]},
        },
      }),
    });
    check.addToRolePolicy(new iam.PolicyStatement({
      actions: ["cloudformation:GetTemplate", "cloudformation:DescribeStacks"],
      resources: [`arn:aws:cloudformation:${this.region}:${this.account}:stack/${WORKLOAD_STACK}/*`],
    }));
    // Synthesizing the workload VPC looks up the Region's availability zones, so the check cannot run without this
    // read. The names are public, the action takes no resource condition, and it neither changes nor reveals data.
    check.addToRolePolicy(new iam.PolicyStatement({ actions: ["ec2:DescribeAvailabilityZones"], resources: ["*"] }));
    check.addToRolePolicy(new iam.PolicyStatement({
      actions: ["codecommit:PostCommentForPullRequest"], resources: [repository.repositoryArn],
    }));

    repository.onPullRequestStateChange("PullRequestCheck", {
      eventPattern: { detail: { event: ["pullRequestCreated", "pullRequestSourceBranchUpdated"] } },
      target: new targets.CodeBuildProject(check, {
        event: events.RuleTargetInput.fromObject({
          sourceVersion: events.EventField.fromPath("$.detail.sourceCommit"),
          environmentVariablesOverride: [
            { name: "PULL_REQUEST_ID", value: events.EventField.fromPath("$.detail.pullRequestId"), type: "PLAINTEXT" },
            { name: "SOURCE_COMMIT", value: events.EventField.fromPath("$.detail.sourceCommit"), type: "PLAINTEXT" },
            { name: "DESTINATION_COMMIT", value: events.EventField.fromPath("$.detail.destinationCommit"), type: "PLAINTEXT" },
          ],
        }),
      }),
    });

    new cdk.CfnOutput(this, "WorkloadRepositoryCloneUrl", { value: repository.repositoryCloneUrlGrc });
    new cdk.CfnOutput(this, "WorkloadRepositoryConsole", {
      value: `https://${this.region}.console.aws.amazon.com/codesuite/codecommit/repositories/${WORKLOAD_REPOSITORY}/browse?region=${this.region}`,
    });
    new cdk.CfnOutput(this, "WorkloadPipelineConsole", {
      value: `https://${this.region}.console.aws.amazon.com/codesuite/codepipeline/pipelines/${WORKLOAD_PIPELINE}/view?region=${this.region}`,
    });
  }
}
