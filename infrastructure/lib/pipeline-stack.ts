import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as sfn_tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
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
}

interface TaskPair {
  taskDef: ecs.FargateTaskDefinition;
  container: ecs.ContainerDefinition;
}

export class StormTrackingPipelineStack extends cdk.Stack {
  public bucket: s3.Bucket;
  public cluster: ecs.Cluster;
  public stateMachine: sfn.StateMachine;

  private vpc: ec2.Vpc;
  private taskSg: ec2.SecurityGroup;
  private cdsSecret: secretsmanager.Secret;
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
      description: 'CDS API credentials — JSON with keys: api_url, api_key',
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
      return ecs.ContainerImage.fromAsset(path.join(TASKS_DIR, directory));
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
      eswd: this.makeTask('Eswd', 'eswd_scraper', 256, 512, {
        ...commonEnv,
        S3_PREFIX: 'raw/eswd/',
      }),

      blitzortung: this.makeTask('Blitzortung', 'blitzortung_scraper', 256, 512, {
        ...commonEnv,
        S3_PREFIX: 'raw/blitzortung/',
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
      this.makeStep('ScrapeEswd', tasks.eswd, { envOverrides: yearOverrides }),
    );
    acquire.branch(
      this.makeStep('ScrapeBlitzortung', tasks.blitzortung, { envOverrides: yearOverrides }),
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

    const chain = acquire.next(featuresStep).next(datasetStep);

    this.stateMachine = new sfn.StateMachine(this, 'Pipeline', {
      stateMachineName: 'storm-tracking-pipeline',
      definitionBody: sfn.DefinitionBody.fromChainable(chain),
      timeout: cdk.Duration.hours(48),
    });
  }

  // ── Outputs ─────────────────────────────────────────────────

  private createOutputs(): void {
    new cdk.CfnOutput(this, 'BucketName', { value: this.bucket.bucketName });
    new cdk.CfnOutput(this, 'StateMachineArn', { value: this.stateMachine.stateMachineArn });
    new cdk.CfnOutput(this, 'ClusterArn', { value: this.cluster.clusterArn });
    new cdk.CfnOutput(this, 'PublicSubnetIds', {
      value: cdk.Fn.join(',', this.vpc.publicSubnets.map(s => s.subnetId)),
    });
    new cdk.CfnOutput(this, 'TaskSecurityGroupId', {
      value: this.taskSg.securityGroupId,
    });
  }
}
