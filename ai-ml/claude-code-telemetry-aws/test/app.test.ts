import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { TelemetryStack } from '../lib/telemetry-stack.js';
import { appConfig } from '../lib/config/app-config.js';

const env = { account: '123456789012', region: appConfig.region };

describe('TelemetryStack (Nested Stack Structure)', () => {
  let app: cdk.App;
  let stack: TelemetryStack;

  beforeAll(() => {
    app = new cdk.App();
    stack = new TelemetryStack(app, 'TestTelemetryStack', { env, config: appConfig });
  });

  test('Root stack synthesizes with nested stacks', () => {
    const template = Template.fromStack(stack);
    // Root stack should contain 5 nested stacks
    template.resourceCountIs('AWS::CloudFormation::Stack', 5);
  });

  test('Root stack has CollectorEndpoint output', () => {
    const template = Template.fromStack(stack);
    template.hasOutput('CollectorEndpoint', {
      Description: 'OTLP gRPC Collector Endpoint',
    });
  });

  test('Root stack has GrafanaEndpoint output', () => {
    const template = Template.fromStack(stack);
    template.hasOutput('GrafanaEndpoint', {
      Description: 'Grafana Workspace URL',
    });
  });

  test('NetworkNestedStack creates VPC', () => {
    const networkStack = stack.node.findChild('Network') as cdk.NestedStack;
    const template = Template.fromStack(networkStack);
    template.resourceCountIs('AWS::EC2::VPC', 1);
  });

  test('MetricsNestedStack creates AMP Workspace', () => {
    const metricsStack = stack.node.findChild('Metrics') as cdk.NestedStack;
    const template = Template.fromStack(metricsStack);
    template.resourceCountIs('AWS::APS::Workspace', 1);
  });

  test('EventsNestedStack creates S3, Firehose, Glue, CW Logs, and Subscription Filter', () => {
    const eventsStack = stack.node.findChild('Events') as cdk.NestedStack;
    const template = Template.fromStack(eventsStack);
    template.resourceCountIs('AWS::S3::Bucket', 2);
    template.resourceCountIs('AWS::KinesisFirehose::DeliveryStream', 1);
    template.resourceCountIs('AWS::Glue::Database', 1);
    template.resourceCountIs('AWS::Glue::Table', 1);
    template.resourceCountIs('AWS::Logs::LogGroup', 2);
    template.resourceCountIs('AWS::Logs::SubscriptionFilter', 1);
    // Glue table must carry the extended 2.x schema: 34 original + 38 new = 72 columns
    template.hasResourceProperties('AWS::Glue::Table', {
      TableInput: {
        StorageDescriptor: {
          // Match.arrayWith requires the matchers to appear in the array in
          // order, so list them in the actual Glue column order.
          Columns: Match.arrayWith([
            Match.objectLike({ Name: 'agent_name' }),
            Match.objectLike({ Name: 'effort' }),
            Match.objectLike({ Name: 'mcp_server_name' }),
            Match.objectLike({ Name: 'hook_name' }),
            Match.objectLike({ Name: 'plugin_name' }),
            Match.objectLike({ Name: 'skill_name' }),
          ]),
        },
      },
    });
  });

  test('CollectorNestedStack creates ECS cluster and NLB', () => {
    const collectorStack = stack.node.findChild('Collector') as cdk.NestedStack;
    const template = Template.fromStack(collectorStack);
    template.resourceCountIs('AWS::ECS::Cluster', 1);
    template.resourceCountIs('AWS::ECS::Service', 1);
    template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 1);
  });

  test('CollectorNestedStack has circuit breaker enabled', () => {
    const collectorStack = stack.node.findChild('Collector') as cdk.NestedStack;
    const template = Template.fromStack(collectorStack);
    template.hasResourceProperties('AWS::ECS::Service', {
      DeploymentConfiguration: {
        DeploymentCircuitBreaker: {
          Enable: true,
          Rollback: true,
        },
      },
    });
  });

  test('DashboardNestedStack creates Grafana workspace', () => {
    const dashboardStack = stack.node.findChild('Dashboard') as cdk.NestedStack;
    const template = Template.fromStack(dashboardStack);
    template.resourceCountIs('AWS::Grafana::Workspace', 1);
  });
});
