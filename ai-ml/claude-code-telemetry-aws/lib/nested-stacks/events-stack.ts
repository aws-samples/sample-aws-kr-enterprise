import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as firehose from 'aws-cdk-lib/aws-kinesisfirehose';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as path from 'path';
import { Construct } from 'constructs';
import { AppConfig, resourceName } from '../config/app-config.js';

export interface EventsNestedStackProps extends cdk.NestedStackProps {
  readonly config: AppConfig;
}

/**
 * EventsNestedStack provisions the event pipeline:
 * Firehose Delivery Stream -> S3 (Parquet) -> Glue Catalog -> Athena queryable.
 *
 * Schema based on the data-engineer design in /docs/data-schema.md.
 */
export class EventsNestedStack extends cdk.NestedStack {
  /** S3 bucket ARN for events storage */
  public readonly eventsBucketArn: string;
  /** S3 bucket name for events storage */
  public readonly eventsBucketName: string;
  /** Firehose delivery stream ARN */
  public readonly firehoseStreamArn: string;
  /** Firehose delivery stream name */
  public readonly firehoseStreamName: string;
  /** Glue database name for Athena queries */
  public readonly glueDatabaseName: string;
  /** Glue table name for Athena queries */
  public readonly glueTableName: string;
  /** CloudWatch Logs log group name for telemetry events */
  public readonly logGroupName: string;
  /** CloudWatch Logs log group ARN for telemetry events */
  public readonly logGroupArn: string;

  constructor(scope: Construct, id: string, props: EventsNestedStackProps) {
    super(scope, id, props);

    const { config } = props;

    // -------------------------------------------------------------------------
    // S3 Access Logging Bucket for audit trail
    // -------------------------------------------------------------------------
    const accessLogsBucket = new s3.Bucket(this, 'AccessLogsBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      lifecycleRules: [
        {
          id: 'DeleteOldAccessLogs',
          expiration: cdk.Duration.days(90),
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // -------------------------------------------------------------------------
    // S3 Bucket with tiered lifecycle per data-schema.md spec
    // -------------------------------------------------------------------------
    const eventsBucket = new s3.Bucket(this, 'EventsBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: false,
      eventBridgeEnabled: true,
      serverAccessLogsBucket: accessLogsBucket,
      serverAccessLogsPrefix: 'events-bucket-logs/',
      lifecycleRules: [
        {
          id: 'TransitionToIA',
          prefix: 'year=',
          transitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: cdk.Duration.days(90),
            },
            {
              storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
              transitionAfter: cdk.Duration.days(365),
            },
          ],
          expiration: cdk.Duration.days(730),
        },
        {
          id: 'DeleteErrors',
          prefix: 'errors/',
          expiration: cdk.Duration.days(30),
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.eventsBucketArn = eventsBucket.bucketArn;
    this.eventsBucketName = eventsBucket.bucketName;

    // -------------------------------------------------------------------------
    // Glue Database
    // -------------------------------------------------------------------------
    const glueDbName = 'claude_code_telemetry';

    const database = new glue.CfnDatabase(this, 'GlueDatabase', {
      catalogId: this.account,
      databaseInput: {
        name: glueDbName,
        description: 'Claude Code telemetry events database',
      },
    });

    this.glueDatabaseName = glueDbName;

    // -------------------------------------------------------------------------
    // Glue Table with full schema from data-schema.md
    // -------------------------------------------------------------------------
    const table = new glue.CfnTable(this, 'EventsTable', {
      catalogId: this.account,
      databaseName: glueDbName,
      tableInput: {
        name: 'events',
        description: 'Claude Code telemetry events (unified schema)',
        tableType: 'EXTERNAL_TABLE',
        parameters: {
          'classification': 'parquet',
          'parquet.compression': 'SNAPPY',
          'has_encrypted_data': 'false',
          // Partition projection disabled - partitions registered via S3 event-driven Lambda
          'projection.enabled': 'false',
        },
        storageDescriptor: {
          location: `s3://${eventsBucket.bucketName}/`,
          inputFormat: 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat',
          outputFormat: 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat',
          serdeInfo: {
            serializationLibrary: 'org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe',
          },
          columns: [
            // Common fields
            { name: 'event_name', type: 'string', comment: 'Event type identifier' },
            { name: 'session_id', type: 'string', comment: 'Session unique identifier' },
            { name: 'timestamp', type: 'timestamp', comment: 'Event timestamp (UTC)' },
            { name: 'organization_id', type: 'string', comment: 'Organization identifier' },
            { name: 'user_id', type: 'string', comment: 'User identifier (hashed)' },
            { name: 'user_name', type: 'string', comment: 'User name from resource attributes' },
            { name: 'terminal_type', type: 'string', comment: 'Terminal type' },
            // Resource attributes
            { name: 'service_name', type: 'string', comment: 'Service name' },
            { name: 'service_version', type: 'string', comment: 'Service version' },
            { name: 'os_type', type: 'string', comment: 'Operating system type' },
            { name: 'os_version', type: 'string', comment: 'Operating system version' },
            { name: 'host_arch', type: 'string', comment: 'Host architecture' },
            // Custom resource attributes
            { name: 'department', type: 'string', comment: 'Department name' },
            { name: 'team_id', type: 'string', comment: 'Team identifier' },
            { name: 'cost_center', type: 'string', comment: 'Cost center code' },
            // Event-specific fields
            { name: 'prompt_length', type: 'int', comment: 'Prompt character length (user_prompt)' },
            { name: 'prompt_id', type: 'string', comment: 'Prompt unique identifier' },
            { name: 'tool_name', type: 'string', comment: 'Tool name (tool_result, tool_decision)' },
            { name: 'success', type: 'boolean', comment: 'Tool execution success (tool_result)' },
            { name: 'duration_ms', type: 'double', comment: 'Execution duration ms (tool_result, api_request, api_error)' },
            { name: 'error', type: 'string', comment: 'Error message (tool_result, api_error)' },
            { name: 'decision', type: 'string', comment: 'Tool decision accept/reject (tool_result, tool_decision)' },
            { name: 'source', type: 'string', comment: 'Decision source (tool_result, tool_decision)' },
            { name: 'tool_parameters', type: 'string', comment: 'Tool parameters JSON string (tool_result)' },
            { name: 'tool_result_size_bytes', type: 'int', comment: 'Tool result size in bytes (tool_result)' },
            { name: 'model', type: 'string', comment: 'Model name (api_request, api_error)' },
            { name: 'speed', type: 'string', comment: 'API response speed mode (api_request)' },
            { name: 'cost_usd', type: 'double', comment: 'API call cost USD (api_request)' },
            { name: 'input_tokens', type: 'bigint', comment: 'Input tokens (api_request)' },
            { name: 'output_tokens', type: 'bigint', comment: 'Output tokens (api_request)' },
            { name: 'cache_read_tokens', type: 'bigint', comment: 'Cache read tokens (api_request)' },
            { name: 'cache_creation_tokens', type: 'bigint', comment: 'Cache creation tokens (api_request)' },
            { name: 'status_code', type: 'int', comment: 'HTTP status code (api_error)' },
            { name: 'attempt', type: 'int', comment: 'Retry attempt number (api_error)' },
            // --- Claude Code 2.x extended schema (verified against live OTLP 2026-06) ---
            // Common
            { name: 'event_sequence', type: 'bigint', comment: 'Monotonic event sequence number' },
            // api_request / api_error: subagent, effort, MCP attribution
            { name: 'agent_name', type: 'string', comment: 'Subagent name (api_request, api_error)' },
            { name: 'effort', type: 'string', comment: 'Effort mode e.g. high/xhigh (api_request, api_error)' },
            { name: 'query_source', type: 'string', comment: 'Query source e.g. repl_main_thread (api_request, api_error)' },
            { name: 'mcp_server_name', type: 'string', comment: 'MCP server name attributed to request (api_request)' },
            { name: 'mcp_tool_name', type: 'string', comment: 'MCP tool name attributed to request (api_request)' },
            { name: 'cost_usd_micros', type: 'bigint', comment: 'API cost in micro-USD (api_request)' },
            { name: 'request_id', type: 'string', comment: 'API request id (api_error)' },
            // tool_result extended
            { name: 'error_type', type: 'string', comment: 'Error category (tool_result)' },
            { name: 'tool_input_size_bytes', type: 'int', comment: 'Tool input size bytes (tool_result)' },
            { name: 'tool_use_id', type: 'string', comment: 'Tool use id (tool_result, tool_decision)' },
            { name: 'mcp_server_scope', type: 'string', comment: 'MCP server scope (tool_result)' },
            // user_prompt extended
            { name: 'command_name', type: 'string', comment: 'Slash command name (user_prompt)' },
            { name: 'command_source', type: 'string', comment: 'Command source (user_prompt)' },
            // hook_* events
            { name: 'hook_name', type: 'string', comment: 'Hook name (hook_execution_*)' },
            { name: 'hook_event', type: 'string', comment: 'Hook lifecycle event e.g. PreToolUse (hook_*)' },
            { name: 'hook_source', type: 'string', comment: 'Hook source e.g. merged/flagSettings (hook_*)' },
            { name: 'hook_type', type: 'string', comment: 'Hook type e.g. command (hook_registered)' },
            { name: 'total_duration_ms', type: 'double', comment: 'Total hook execution duration ms (hook_execution_complete)' },
            { name: 'num_hooks', type: 'int', comment: 'Number of hooks (hook_execution_*)' },
            { name: 'num_success', type: 'int', comment: 'Successful hooks (hook_execution_complete)' },
            { name: 'num_blocking', type: 'int', comment: 'Blocking hooks (hook_execution_complete)' },
            { name: 'num_cancelled', type: 'int', comment: 'Cancelled hooks (hook_execution_complete)' },
            { name: 'num_non_blocking_error', type: 'int', comment: 'Non-blocking hook errors (hook_execution_complete)' },
            // plugin_loaded / skill_activated / mcp_server_connection
            { name: 'plugin_name', type: 'string', comment: 'Plugin name (plugin_loaded, skill_activated, mcp_server_connection)' },
            { name: 'plugin_scope', type: 'string', comment: 'Plugin scope e.g. official (plugin_loaded)' },
            { name: 'plugin_version', type: 'string', comment: 'Plugin version (plugin_loaded)' },
            { name: 'marketplace_name', type: 'string', comment: 'Marketplace name (plugin_loaded, skill_activated)' },
            { name: 'enabled_via', type: 'string', comment: 'How plugin was enabled (plugin_loaded)' },
            { name: 'has_mcp', type: 'boolean', comment: 'Plugin provides MCP (plugin_loaded)' },
            { name: 'has_hooks', type: 'boolean', comment: 'Plugin provides hooks (plugin_loaded)' },
            { name: 'skill_name', type: 'string', comment: 'Skill name (skill_activated)' },
            { name: 'skill_source', type: 'string', comment: 'Skill source e.g. plugin (skill_activated)' },
            { name: 'invocation_trigger', type: 'string', comment: 'Skill invocation trigger (skill_activated)' },
            { name: 'transport_type', type: 'string', comment: 'MCP transport e.g. stdio (mcp_server_connection)' },
            { name: 'mcp_status', type: 'string', comment: 'MCP connection status e.g. connected (mcp_server_connection)' },
            { name: 'server_scope', type: 'string', comment: 'MCP server scope (mcp_server_connection)' },
            { name: 'is_plugin', type: 'boolean', comment: 'MCP server came from a plugin (mcp_server_connection)' },
            // subagent_completed: per-subagent token/tool/duration attribution
            { name: 'agent_type', type: 'string', comment: 'Subagent type e.g. general-purpose (subagent_completed)' },
            { name: 'agent_source', type: 'string', comment: 'Subagent source e.g. built-in (subagent_completed)' },
            { name: 'is_built_in', type: 'boolean', comment: 'Subagent is built-in (subagent_completed)' },
            { name: 'is_async', type: 'boolean', comment: 'Subagent ran async (subagent_completed)' },
            { name: 'total_tokens', type: 'bigint', comment: 'Total tokens used by subagent (subagent_completed)' },
            { name: 'total_tool_uses', type: 'int', comment: 'Total tool uses by subagent (subagent_completed)' },
          ],
        },
        partitionKeys: [
          { name: 'year', type: 'string', comment: 'Year (YYYY)' },
          { name: 'month', type: 'string', comment: 'Month (MM)' },
          { name: 'day', type: 'string', comment: 'Day (DD)' },
          { name: 'hour', type: 'string', comment: 'Hour (HH, UTC)' },
        ],
      },
    });

    table.addDependency(database);

    this.glueTableName = 'events';

    // -------------------------------------------------------------------------
    // IAM Role for Firehose
    // -------------------------------------------------------------------------
    const firehoseRole = new iam.Role(this, 'FirehoseRole', {
      roleName: resourceName(config, 'firehose-role'),
      assumedBy: new iam.ServicePrincipal('firehose.amazonaws.com'),
    });

    eventsBucket.grantReadWrite(firehoseRole);

    firehoseRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'glue:GetTable',
          'glue:GetTableVersion',
          'glue:GetTableVersions',
        ],
        resources: [
          `arn:aws:glue:${config.region}:${this.account}:catalog`,
          `arn:aws:glue:${config.region}:${this.account}:database/${glueDbName}`,
          `arn:aws:glue:${config.region}:${this.account}:table/${glueDbName}/*`,
        ],
      }),
    );

    // CloudWatch Logs for Firehose error logging
    const firehoseLogGroup = new logs.LogGroup(this, 'FirehoseLogGroup', {
      logGroupName: `/aws/firehose/${resourceName(config, 'events-stream')}`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    firehoseRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'logs:PutLogEvents',
          'logs:CreateLogStream',
        ],
        resources: [firehoseLogGroup.logGroupArn],
      }),
    );

    // -------------------------------------------------------------------------
    // Lambda Transformation: CW Logs envelope -> flat JSON for Parquet
    // -------------------------------------------------------------------------
    // CW Logs Subscription Filter sends data in gzip+base64 envelope format.
    // This Lambda decodes the envelope, extracts OTLP log records from ADOT,
    // and flattens them into JSON matching the Glue schema for Parquet conversion.
    const transformerFn = new lambda.Function(this, 'FirehoseTransformerFn', {
      functionName: resourceName(config, 'firehose-transformer'),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'firehose-transformer')),
      timeout: cdk.Duration.minutes(3),
      memorySize: 256,
    });

    // Grant Firehose permission to invoke the Lambda
    transformerFn.grantInvoke(firehoseRole);

    // -------------------------------------------------------------------------
    // Firehose Delivery Stream with Parquet conversion + hour partitioning
    // -------------------------------------------------------------------------
    const stream = new firehose.CfnDeliveryStream(this, 'EventsStream', {
      deliveryStreamName: resourceName(config, 'events-stream'),
      deliveryStreamType: 'DirectPut',
      extendedS3DestinationConfiguration: {
        bucketArn: eventsBucket.bucketArn,
        roleArn: firehoseRole.roleArn,
        prefix: 'year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/',
        errorOutputPrefix: 'errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/',
        bufferingHints: {
          intervalInSeconds: 300,
          sizeInMBs: 128,
        },
        compressionFormat: 'UNCOMPRESSED', // Parquet handles its own compression via SerDe
        cloudWatchLoggingOptions: {
          enabled: true,
          logGroupName: firehoseLogGroup.logGroupName,
          logStreamName: 'S3Delivery',
        },
        processingConfiguration: {
          enabled: true,
          processors: [
            {
              type: 'Lambda',
              parameters: [
                {
                  parameterName: 'LambdaArn',
                  parameterValue: transformerFn.functionArn,
                },
                {
                  parameterName: 'BufferSizeInMBs',
                  parameterValue: '3',
                },
                {
                  parameterName: 'BufferIntervalInSeconds',
                  parameterValue: '60',
                },
              ],
            },
          ],
        },
        dynamicPartitioningConfiguration: {
          enabled: false,
        },
        dataFormatConversionConfiguration: {
          enabled: true,
          inputFormatConfiguration: {
            deserializer: {
              openXJsonSerDe: {},
            },
          },
          outputFormatConfiguration: {
            serializer: {
              parquetSerDe: {
                compression: 'SNAPPY',
              },
            },
          },
          schemaConfiguration: {
            catalogId: this.account,
            databaseName: glueDbName,
            tableName: 'events',
            region: config.region,
            roleArn: firehoseRole.roleArn,
            versionId: 'LATEST',
          },
        },
      },
    });

    // Firehose depends on the Glue table for schema-based format conversion
    stream.addDependency(table);

    // Ensure IAM policies are fully propagated before Firehose creation
    const defaultPolicy = firehoseRole.node.findChild('DefaultPolicy').node.defaultChild as cdk.CfnResource;
    if (defaultPolicy) {
      stream.addDependency(defaultPolicy);
    }

    this.firehoseStreamArn = stream.attrArn;
    this.firehoseStreamName = resourceName(config, 'events-stream');

    // -------------------------------------------------------------------------
    // CloudWatch Logs Group for telemetry events (ADOT -> CW Logs -> Firehose)
    // -------------------------------------------------------------------------
    const telemetryLogGroup = new logs.LogGroup(this, 'TelemetryEventsLogGroup', {
      logGroupName: '/claude-code/telemetry-events',
      retention: logs.RetentionDays.ONE_DAY,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.logGroupName = telemetryLogGroup.logGroupName;
    this.logGroupArn = telemetryLogGroup.logGroupArn;

    // -------------------------------------------------------------------------
    // IAM Role for CW Logs Subscription Filter -> Firehose
    // -------------------------------------------------------------------------
    const subscriptionFilterRole = new iam.Role(this, 'SubscriptionFilterRole', {
      roleName: resourceName(config, 'cw-to-firehose-role'),
      assumedBy: new iam.ServicePrincipal(`logs.${config.region}.amazonaws.com`),
    });

    subscriptionFilterRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'firehose:PutRecord',
          'firehose:PutRecordBatch',
        ],
        resources: [stream.attrArn],
      }),
    );

    // -------------------------------------------------------------------------
    // Subscription Filter: CW Logs -> Firehose delivery stream
    // -------------------------------------------------------------------------
    const subscriptionFilter = new logs.CfnSubscriptionFilter(this, 'TelemetrySubscriptionFilter', {
      logGroupName: telemetryLogGroup.logGroupName,
      filterPattern: '',
      destinationArn: stream.attrArn,
      roleArn: subscriptionFilterRole.roleArn,
    });

    // Ensure IAM policy is propagated before Subscription Filter creation
    // (CW Logs validates the role by sending a test message at creation time)
    const subFilterPolicy = subscriptionFilterRole.node.findChild('DefaultPolicy').node.defaultChild as cdk.CfnResource;
    if (subFilterPolicy) {
      subscriptionFilter.addDependency(subFilterPolicy);
    }

    // -------------------------------------------------------------------------
    // Partition Registration: S3 Event -> EventBridge -> Lambda -> Glue BatchCreatePartition
    // -------------------------------------------------------------------------
    // When Firehose writes a new Parquet file to S3, EventBridge receives an
    // Object Created event. This Lambda extracts the partition values from the
    // S3 key and registers the partition in Glue via BatchCreatePartition.
    // Duplicate partitions are handled gracefully (AlreadyExistsException → skip).
    const partitionRegisterFn = new lambda.Function(this, 'PartitionRegisterFn', {
      functionName: resourceName(config, 'partition-register'),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import boto3, os, re, json

glue = boto3.client('glue')

DB = os.environ['GLUE_DATABASE']
TABLE = os.environ['GLUE_TABLE']
BUCKET = os.environ['EVENTS_BUCKET']

PARTITION_RE = re.compile(
    r'year=(?P<year>\\d{4})/month=(?P<month>\\d{2})/day=(?P<day>\\d{2})/hour=(?P<hour>\\d{2})/'
)

def handler(event, context):
    key = event['detail']['object']['key']

    m = PARTITION_RE.search(key)
    if not m:
        print(f'Skipping key with no partition pattern: {key}')
        return {'status': 'skipped', 'key': key}

    year, month, day, hour = m.group('year'), m.group('month'), m.group('day'), m.group('hour')
    location = f's3://{BUCKET}/year={year}/month={month}/day={day}/hour={hour}/'

    # Get table StorageDescriptor so partition inherits the same Serde/format
    table_resp = glue.get_table(DatabaseName=DB, Name=TABLE)
    sd = table_resp['Table']['StorageDescriptor']

    partition_input = {
        'Values': [year, month, day, hour],
        'StorageDescriptor': {
            'Columns': sd['Columns'],
            'Location': location,
            'InputFormat': sd['InputFormat'],
            'OutputFormat': sd['OutputFormat'],
            'SerdeInfo': sd['SerdeInfo'],
            'Compressed': sd.get('Compressed', False),
            'StoredAsSubDirectories': sd.get('StoredAsSubDirectories', False),
        },
    }

    try:
        glue.batch_create_partition(
            DatabaseName=DB,
            TableName=TABLE,
            PartitionInputList=[partition_input],
        )
        print(f'Registered partition: year={year}/month={month}/day={day}/hour={hour}')
        return {'status': 'created', 'partition': [year, month, day, hour]}
    except glue.exceptions.AlreadyExistsException:
        print(f'Partition already exists: year={year}/month={month}/day={day}/hour={hour}')
        return {'status': 'exists', 'partition': [year, month, day, hour]}
`),
      timeout: cdk.Duration.seconds(30),
      memorySize: 128,
      environment: {
        GLUE_DATABASE: glueDbName,
        GLUE_TABLE: 'events',
        EVENTS_BUCKET: eventsBucket.bucketName,
      },
    });

    partitionRegisterFn.role!.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          'glue:BatchCreatePartition',
          'glue:GetTable',
        ],
        resources: [
          `arn:aws:glue:${config.region}:${this.account}:catalog`,
          `arn:aws:glue:${config.region}:${this.account}:database/${glueDbName}`,
          `arn:aws:glue:${config.region}:${this.account}:table/${glueDbName}/events`,
        ],
      }),
    );
    // EventBridge rule: match S3 Object Created events for the events bucket
    new events.Rule(this, 'PartitionRegisterRule', {
      ruleName: resourceName(config, 'partition-register-rule'),
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Created'],
        detail: {
          bucket: {
            name: [eventsBucket.bucketName],
          },
          object: {
            key: [{ prefix: 'year=' }],
          },
        },
      },
      targets: [new targets.LambdaFunction(partitionRegisterFn)],
    });
  }
}
