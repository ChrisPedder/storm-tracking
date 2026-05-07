import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatch_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as events from 'aws-cdk-lib/aws-events';
import * as events_targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as sfn_tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sns_subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import { Construct } from 'constructs';

const TASKS_DIR = path.join(__dirname, '..', '..', 'tasks');

const SWITZERLAND_BBOX: Record<string, string> = {
  north: '48.3',
  south: '45.3',
  west: '5.5',
  east: '11.0',
};

export interface StormTrackingPipelineStackProps extends cdk.StackProps {
  /**
   * When false, task definitions use a placeholder container image instead
   * of building Docker images from the tasks/ directory. Set to false in
   * unit tests to avoid requiring a Docker daemon.
   * @default true
   */
  readonly useDockerAssets?: boolean;

  /**
   * Email address for pipeline failure and cost alerts.
   * @default - no notifications
   */
  readonly alertEmail?: string;

  /**
   * Monthly cost alarm threshold in USD for Fargate tasks.
   * @default 50
   */
  readonly costAlarmThresholdUsd?: number;
}

interface TaskPair {
  taskDef: ecs.FargateTaskDefinition;
  container: ecs.ContainerDefinition;
}

export class StormTrackingPipelineStack extends cdk.Stack {
  public bucket: s3.Bucket;
  public cluster: ecs.Cluster;
  public stateMachine: sfn.StateMachine;
  public forecastStateMachine: sfn.StateMachine;

  private vpc: ec2.Vpc;
  private taskSg: ec2.SecurityGroup;
  private cdsSecret: secretsmanager.Secret;
  private eumetsatSecret: secretsmanager.Secret;
  private logGroup: logs.LogGroup;
  private taskRole: iam.Role;
  private readonly useDockerAssets: boolean;

  constructor(scope: Construct, id: string, props?: StormTrackingPipelineStackProps) {
    super(scope, id, props);
    this.useDockerAssets = props?.useDockerAssets ?? true;

    this.createNetworking();
    this.createStorage();
    this.createSecrets();
    this.createLogging();
    this.createCluster();
    this.createTaskRole();
    const tasks = this.createTaskDefinitions();
    this.createStateMachine(tasks);
    this.createForecastStateMachine(tasks);
    this.createSchedule();
    this.createAlarms(props?.alertEmail, props?.costAlarmThresholdUsd ?? 50);
    this.createOutputs();
  }

  // ── Networking ──────────────────────────────────────────────

  private createNetworking(): void {
    this.vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
      ],
      gatewayEndpoints: {
        S3: { service: ec2.GatewayVpcEndpointAwsService.S3 },
      },
    });

    this.taskSg = new ec2.SecurityGroup(this, 'TaskSg', {
      vpc: this.vpc,
      description: 'Fargate pipeline tasks - outbound only',
      allowAllOutbound: true,
    });
  }

  // ── Storage ─────────────────────────────────────────────────

  private createStorage(): void {
    this.bucket = new s3.Bucket(this, 'DataBucket', {
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
    });
  }

  // ── Secrets ─────────────────────────────────────────────────

  private createSecrets(): void {
    this.cdsSecret = new secretsmanager.Secret(this, 'CdsApiKey', {
      secretName: 'storm-tracking/cds-api-key',
      description: 'CDS API credentials - JSON with keys: api_url, api_key',
    });

    this.eumetsatSecret = new secretsmanager.Secret(this, 'EumetsatApiKey', {
      secretName: 'storm-tracking/eumetsat-api-key',
      description: 'EUMETSAT Data Store credentials - JSON with keys: consumer_key, consumer_secret',
    });
  }

  // ── Logging ─────────────────────────────────────────────────

  private createLogging(): void {
    this.logGroup = new logs.LogGroup(this, 'PipelineLogs', {
      logGroupName: '/storm-tracking/pipeline',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
  }

  // ── ECS Cluster ─────────────────────────────────────────────

  private createCluster(): void {
    this.cluster = new ecs.Cluster(this, 'Cluster', { vpc: this.vpc });
  }

  // ── IAM ─────────────────────────────────────────────────────

  private createTaskRole(): void {
    this.taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    });
    this.bucket.grantReadWrite(this.taskRole);
  }

  // ── Helpers ─────────────────────────────────────────────────

  private containerImage(directory: string): ecs.ContainerImage {
    if (this.useDockerAssets) {
      return ecs.ContainerImage.fromAsset(path.join(TASKS_DIR, directory), {
        platform: cdk.aws_ecr_assets.Platform.LINUX_AMD64,
      });
    }
    return ecs.ContainerImage.fromRegistry('public.ecr.aws/docker/library/python:3.11-slim');
  }

  private makeTask(
    name: string,
    directory: string,
    cpu: number,
    memoryMib: number,
    environment: Record<string, string>,
    options?: {
      secrets?: Record<string, ecs.Secret>;
      ephemeralStorageGiB?: number;
    },
  ): TaskPair {
    const taskDef = new ecs.FargateTaskDefinition(this, `${name}TaskDef`, {
      family: `storm-tracking-${directory.replace(/_/g, '-')}`,
      cpu,
      memoryLimitMiB: memoryMib,
      taskRole: this.taskRole,
      ...(options?.ephemeralStorageGiB !== undefined && {
        ephemeralStorageGiB: options.ephemeralStorageGiB,
      }),
    });

    const container = taskDef.addContainer(`${name}Container`, {
      image: this.containerImage(directory),
      logging: ecs.LogDriver.awsLogs({
        streamPrefix: directory.replace(/_/g, '-'),
        logGroup: this.logGroup,
      }),
      environment,
      secrets: options?.secrets ?? {},
    });

    return { taskDef, container };
  }

  private makeStep(
    stepId: string,
    task: TaskPair,
    options?: {
      envOverrides?: sfn_tasks.TaskEnvironmentVariable[];
      timeoutHours?: number;
    },
  ): sfn_tasks.EcsRunTask {
    const overrides = options?.envOverrides
      ? [{ containerDefinition: task.container, environment: options.envOverrides }]
      : undefined;

    const step = new sfn_tasks.EcsRunTask(this, stepId, {
      integrationPattern: sfn.IntegrationPattern.RUN_JOB,
      cluster: this.cluster,
      taskDefinition: task.taskDef,
      launchTarget: new sfn_tasks.EcsFargateLaunchTarget({
        platformVersion: ecs.FargatePlatformVersion.LATEST,
      }),
      containerOverrides: overrides,
      subnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [this.taskSg],
      assignPublicIp: true,
      taskTimeout: sfn.Timeout.duration(
        cdk.Duration.hours(options?.timeoutHours ?? 6),
      ),
    });

    step.addRetry({
      maxAttempts: 2,
      interval: cdk.Duration.seconds(60),
      backoffRate: 2,
    });

    return step;
  }

  // ── Task Definitions ────────────────────────────────────────

  private createTaskDefinitions(): Record<string, TaskPair> {
    const commonEnv: Record<string, string> = {
      S3_BUCKET: this.bucket.bucketName,
      BBOX_NORTH: SWITZERLAND_BBOX.north,
      BBOX_SOUTH: SWITZERLAND_BBOX.south,
      BBOX_WEST: SWITZERLAND_BBOX.west,
      BBOX_EAST: SWITZERLAND_BBOX.east,
    };

    return {
      lightning: this.makeTask('Lightning', 'eumetsat_lightning', 512, 1024, {
        ...commonEnv,
        S3_PREFIX: 'raw/lightning/',
      }, {
        secrets: {
          EUMETSAT_KEY: ecs.Secret.fromSecretsManager(this.eumetsatSecret, 'consumer_key'),
          EUMETSAT_SECRET: ecs.Secret.fromSecretsManager(this.eumetsatSecret, 'consumer_secret'),
        },
      }),

      era5: this.makeTask('Era5', 'era5_downloader', 1024, 4096, {
        ...commonEnv,
        S3_PREFIX: 'raw/era5/',
      }, {
        secrets: {
          CDS_API_KEY: ecs.Secret.fromSecretsManager(this.cdsSecret, 'api_key'),
          CDS_API_URL: ecs.Secret.fromSecretsManager(this.cdsSecret, 'api_url'),
        },
        ephemeralStorageGiB: 100,
      }),

      topo: this.makeTask('Topo', 'topo_downloader', 256, 512, {
        ...commonEnv,
        S3_PREFIX: 'raw/topography/',
      }),

      features: this.makeTask('Features', 'feature_engineer', 4096, 16384, {
        S3_BUCKET: this.bucket.bucketName,
        RAW_PREFIX: 'raw/',
        OUTPUT_PREFIX: 'processed/',
      }, {
        ephemeralStorageGiB: 50,
      }),

      dataset: this.makeTask('Dataset', 'dataset_builder', 2048, 8192, {
        S3_BUCKET: this.bucket.bucketName,
        INPUT_PREFIX: 'processed/',
        OUTPUT_PREFIX: 'output/',
      }),

      trainer: this.makeTask('Trainer', 'model_trainer', 2048, 8192, {
        S3_BUCKET: this.bucket.bucketName,
        INPUT_PREFIX: 'output/',
        OUTPUT_PREFIX: 'output/model/',
      }),

      forecast: this.makeTask('Forecast', 'storm_forecast', 1024, 2048, {
        S3_BUCKET: this.bucket.bucketName,
        MODEL_PREFIX: 'output/model/',
        OUTPUT_PREFIX: 'forecast/',
        BBOX_NORTH: SWITZERLAND_BBOX.north,
        BBOX_SOUTH: SWITZERLAND_BBOX.south,
        BBOX_WEST: SWITZERLAND_BBOX.west,
        BBOX_EAST: SWITZERLAND_BBOX.east,
      }),
    };
  }

  // ── Step Functions ──────────────────────────────────────────

  private createStateMachine(tasks: Record<string, TaskPair>): void {
    const yearOverrides: sfn_tasks.TaskEnvironmentVariable[] = [
      { name: 'START_YEAR', value: sfn.JsonPath.stringAt('$.start_year') },
      { name: 'END_YEAR', value: sfn.JsonPath.stringAt('$.end_year') },
    ];

    const acquire = new sfn.Parallel(this, 'AcquireData', {
      resultPath: sfn.JsonPath.DISCARD,
    });

    acquire.branch(
      this.makeStep('DownloadLightning', tasks.lightning, { envOverrides: yearOverrides }),
    );
    acquire.branch(
      this.makeStep('DownloadEra5', tasks.era5, {
        envOverrides: yearOverrides,
        timeoutHours: 24,
      }),
    );
    acquire.branch(
      this.makeStep('DownloadTopography', tasks.topo),
    );

    const featuresStep = this.makeStep('EngineerFeatures', tasks.features, {
      timeoutHours: 12,
    });
    const datasetStep = this.makeStep('BuildDataset', tasks.dataset);
    const trainerStep = this.makeStep('TrainModel', tasks.trainer, {
      timeoutHours: 2,
    });

    const chain = acquire.next(featuresStep).next(datasetStep).next(trainerStep);

    this.stateMachine = new sfn.StateMachine(this, 'Pipeline', {
      stateMachineName: 'storm-tracking-pipeline',
      definitionBody: sfn.DefinitionBody.fromChainable(chain),
      timeout: cdk.Duration.hours(48),
    });
  }

  // ── Forecast State Machine ───────────────────────────────────

  private createForecastStateMachine(tasks: Record<string, TaskPair>): void {
    const forecastStep = this.makeStep('RunForecast', tasks.forecast, {
      timeoutHours: 1,
    });

    this.forecastStateMachine = new sfn.StateMachine(this, 'ForecastPipeline', {
      stateMachineName: 'storm-tracking-forecast',
      definitionBody: sfn.DefinitionBody.fromChainable(forecastStep),
      timeout: cdk.Duration.hours(2),
    });

    const rule = new events.Rule(this, 'ForecastTrigger', {
      ruleName: 'storm-tracking-forecast',
      schedule: events.Schedule.cron({ hour: '7,19', minute: '0' }),
      enabled: false,
    });
    rule.addTarget(new events_targets.SfnStateMachine(this.forecastStateMachine));
  }

  // ── Schedule ────────────────────────────────────────────────

  private createSchedule(): void {
    const rule = new events.Rule(this, 'MonthlyTrigger', {
      ruleName: 'storm-tracking-monthly',
      schedule: events.Schedule.cron({ day: '1', hour: '3', minute: '0' }),
      enabled: false,
    });

    const currentYear = new Date().getFullYear().toString();
    rule.addTarget(new events_targets.SfnStateMachine(this.stateMachine, {
      input: events.RuleTargetInput.fromObject({
        start_year: currentYear,
        end_year: currentYear,
      }),
    }));
  }

  // ── Alarms & Notifications ─────────────────────────────────

  private createAlarms(alertEmail?: string, costThreshold: number = 50): void {
    const topic = new sns.Topic(this, 'AlertTopic', {
      topicName: 'storm-tracking-alerts',
    });

    if (alertEmail) {
      topic.addSubscription(new sns_subscriptions.EmailSubscription(alertEmail));
    }

    const failureMetric = this.stateMachine.metricFailed({
      period: cdk.Duration.hours(1),
    });
    const failureAlarm = new cloudwatch.Alarm(this, 'PipelineFailureAlarm', {
      alarmName: 'storm-tracking-pipeline-failure',
      metric: failureMetric,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    failureAlarm.addAlarmAction(new cloudwatch_actions.SnsAction(topic));

    const costAlarm = new cloudwatch.Alarm(this, 'CostAlarm', {
      alarmName: 'storm-tracking-cost',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/Billing',
        metricName: 'EstimatedCharges',
        dimensionsMap: { Currency: 'USD' },
        statistic: 'Maximum',
        period: cdk.Duration.hours(6),
      }),
      threshold: costThreshold,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    costAlarm.addAlarmAction(new cloudwatch_actions.SnsAction(topic));
  }

  // ── Outputs ─────────────────────────────────────────────────

  private createOutputs(): void {
    new cdk.CfnOutput(this, 'BucketName', { value: this.bucket.bucketName });
    new cdk.CfnOutput(this, 'StateMachineArn', { value: this.stateMachine.stateMachineArn });
    new cdk.CfnOutput(this, 'ForecastStateMachineArn', { value: this.forecastStateMachine.stateMachineArn });
    new cdk.CfnOutput(this, 'ClusterArn', { value: this.cluster.clusterArn });
    new cdk.CfnOutput(this, 'PublicSubnetIds', {
      value: cdk.Fn.join(',', this.vpc.publicSubnets.map(s => s.subnetId)),
    });
    new cdk.CfnOutput(this, 'TaskSecurityGroupId', {
      value: this.taskSg.securityGroupId,
    });
  }
}
