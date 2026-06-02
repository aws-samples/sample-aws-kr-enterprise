import * as cdk from 'aws-cdk-lib';
import * as grafana from 'aws-cdk-lib/aws-grafana';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { AppConfig, resourceName } from '../config/app-config.js';

export interface DashboardNestedStackProps extends cdk.NestedStackProps {
  readonly config: AppConfig;
  /** AMP workspace ARN from MetricsNestedStack */
  readonly ampWorkspaceArn: string;
  /** AMP workspace ID from MetricsNestedStack */
  readonly ampWorkspaceId: string;
  /** Glue database name from EventsNestedStack */
  readonly glueDatabaseName: string;
  /** S3 events bucket ARN from EventsNestedStack */
  readonly eventsBucketArn: string;
}

/**
 * DashboardNestedStack provisions the Amazon Managed Grafana workspace
 * with data sources configured for AMP (metrics) and Athena (events).
 */
export class DashboardNestedStack extends cdk.NestedStack {
  /** Grafana workspace endpoint URL */
  public readonly grafanaEndpoint: string;

  constructor(scope: Construct, id: string, props: DashboardNestedStackProps) {
    super(scope, id, props);

    const { config } = props;

    // IAM Role for Grafana workspace
    const grafanaRole = new iam.Role(this, 'GrafanaRole', {
      roleName: resourceName(config, 'grafana-role'),
      assumedBy: new iam.ServicePrincipal('grafana.amazonaws.com'),
    });

    // AMP read permissions for Grafana
    grafanaRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'aps:QueryMetrics',
          'aps:GetSeries',
          'aps:GetLabels',
          'aps:GetMetricMetadata',
        ],
        resources: [props.ampWorkspaceArn],
      }),
    );

    // Athena query permissions for Grafana
    grafanaRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'athena:GetQueryExecution',
          'athena:GetQueryResults',
          'athena:StartQueryExecution',
          'athena:StopQueryExecution',
          'athena:GetWorkGroup',
        ],
        resources: [`arn:aws:athena:${config.region}:${this.account}:workgroup/*`],
      }),
    );

    // Athena catalog permissions for Grafana (required by Athena plugin UI)
    grafanaRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'athena:ListDatabases',
          'athena:ListDataCatalogs',
          'athena:ListTableMetadata',
          'athena:GetDatabase',
          'athena:GetDataCatalog',
          'athena:GetTableMetadata',
        ],
        resources: [
          `arn:aws:athena:${config.region}:${this.account}:datacatalog/*`,
        ],
      }),
    );

    // Glue catalog read access for Athena
    grafanaRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'glue:GetDatabase',
          'glue:GetDatabases',
          'glue:GetTable',
          'glue:GetTables',
          'glue:GetPartitions',
        ],
        resources: [
          `arn:aws:glue:${config.region}:${this.account}:catalog`,
          `arn:aws:glue:${config.region}:${this.account}:database/${props.glueDatabaseName}`,
          `arn:aws:glue:${config.region}:${this.account}:table/${props.glueDatabaseName}/*`,
        ],
      }),
    );

    // S3 read access for Athena query results and source data
    grafanaRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          's3:GetObject',
          's3:ListBucket',
          's3:GetBucketLocation',
          's3:PutObject',
        ],
        resources: [
          props.eventsBucketArn,
          `${props.eventsBucketArn}/*`,
          // Athena query results bucket (default location)
          `arn:aws:s3:::aws-athena-query-results-${this.account}-${config.region}`,
          `arn:aws:s3:::aws-athena-query-results-${this.account}-${config.region}/*`,
          // Athena workgroup output location configured on the Grafana data source.
          // Required so Athena can verify/write query results to this bucket.
          ...(config.athenaResultsBucket
            ? [
                `arn:aws:s3:::${config.athenaResultsBucket}`,
                `arn:aws:s3:::${config.athenaResultsBucket}/*`,
              ]
            : []),
        ],
      }),
    );

    // Amazon Managed Grafana Workspace
    const workspace = new grafana.CfnWorkspace(this, 'GrafanaWorkspace', {
      name: resourceName(config, 'grafana'),
      description: 'Claude Code Telemetry Observability Dashboard',
      accountAccessType: 'CURRENT_ACCOUNT',
      authenticationProviders: ['AWS_SSO'],
      permissionType: 'SERVICE_MANAGED',
      roleArn: grafanaRole.roleArn,
      dataSources: ['PROMETHEUS', 'ATHENA'],
      grafanaVersion: '10.4',
    });

    this.grafanaEndpoint = workspace.attrEndpoint;
  }
}
