#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { IncidentToPrPlatformStack } from "../lib/platform-stack";
import { GitOpsStack } from "../lib/gitops-stack";

const app = new cdk.App();
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION || "ap-northeast-2" };

// Independent stacks: the platform finds the workload repository and pipeline by name, and workloads through
// their DynamoDB registration. The sample workload itself (IncidentToPrSampleAppStack) is deployed by the GitOps pipeline.
new GitOpsStack(app, "IncidentToPrGitOpsStack", { env });
new IncidentToPrPlatformStack(app, "IncidentToPrPlatformStack", { env });
