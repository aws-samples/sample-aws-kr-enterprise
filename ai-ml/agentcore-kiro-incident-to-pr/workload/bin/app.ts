#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { SampleAppStack } from "../lib/sample-app-stack";

const app = new cdk.App();
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION || "ap-northeast-2" };
new SampleAppStack(app, "IncidentToPrSampleAppStack", { env });
