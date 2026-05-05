import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { StormTrackingPipelineStack } from '../lib/pipeline-stack';

describe('StormTrackingPipelineStack', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new StormTrackingPipelineStack(app, 'TestStack', {
      useDockerAssets: false,
    });
    template = Template.fromStack(stack);
  });

  describe('networking', () => {
    test('creates a VPC', () => {
      template.resourceCountIs('AWS::EC2::VPC', 1);
    });

    test('creates no NAT gateways', () => {
      template.resourceCountIs('AWS::EC2::NatGateway', 0);
    });

    test('creates an S3 VPC gateway endpoint', () => {
      template.hasResourceProperties('AWS::EC2::VPCEndpoint', {
        ServiceName: Match.objectLike({
          'Fn::Join': Match.arrayWith([
            Match.arrayWith([
              Match.stringLikeRegexp('com\\.amazonaws\\.'),
              Match.stringLikeRegexp('s3'),
            ]),
          ]),
        }),
        VpcEndpointType: 'Gateway',
      });
    });

    test('creates a security group allowing all outbound', () => {
      template.hasResourceProperties('AWS::EC2::SecurityGroup', {
        GroupDescription: Match.stringLikeRegexp('outbound only'),
      });
    });
  });

  describe('storage', () => {
    test('creates an S3 bucket with public access blocked', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
      });
    });

    test('creates an S3 bucket with S3-managed encryption', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        BucketEncryption: {
          ServerSideEncryptionConfiguration: [
            {
              ServerSideEncryptionByDefault: {
                SSEAlgorithm: 'AES256',
              },
            },
          ],
        },
      });
    });
  });

  describe('secrets', () => {
    test('creates a Secrets Manager secret for the CDS API key', () => {
      template.hasResourceProperties('AWS::SecretsManager::Secret', {
        Name: 'storm-tracking/cds-api-key',
      });
    });
  });

  describe('logging', () => {
    test('creates a CloudWatch log group with one month retention', () => {
      template.hasResourceProperties('AWS::Logs::LogGroup', {
        LogGroupName: '/storm-tracking/pipeline',
        RetentionInDays: 30,
      });
    });
  });

  describe('ECS', () => {
    test('creates an ECS cluster', () => {
      template.resourceCountIs('AWS::ECS::Cluster', 1);
    });

    test('creates five Fargate task definitions', () => {
      template.resourceCountIs('AWS::ECS::TaskDefinition', 5);
    });

    test('ERA5 task has 1024 CPU and 4096 MiB memory', () => {
      template.hasResourceProperties('AWS::ECS::TaskDefinition', {
        Cpu: '1024',
        Memory: '4096',
        EphemeralStorage: { SizeInGiB: 100 },
      });
    });

    test('feature engineering task has 4096 CPU and 16384 MiB memory', () => {
      template.hasResourceProperties('AWS::ECS::TaskDefinition', {
        Cpu: '4096',
        Memory: '16384',
        EphemeralStorage: { SizeInGiB: 50 },
      });
    });

    test('dataset builder task has 2048 CPU and 8192 MiB memory', () => {
      template.hasResourceProperties('AWS::ECS::TaskDefinition', {
        Cpu: '2048',
        Memory: '8192',
      });
    });

    test('scraper tasks have 256 CPU and 512 MiB memory', () => {
      const taskDefs = template.findResources('AWS::ECS::TaskDefinition', {
        Properties: { Cpu: '256', Memory: '512' },
      });
      expect(Object.keys(taskDefs).length).toBe(2);
    });
  });

  describe('Step Functions', () => {
    test('creates a state machine named storm-tracking-pipeline', () => {
      template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
        StateMachineName: 'storm-tracking-pipeline',
      });
    });
  });

  describe('IAM', () => {
    test('task role grants S3 read/write access', () => {
      template.hasResourceProperties('AWS::IAM::Policy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                's3:GetObject*',
                's3:GetBucket*',
                's3:List*',
                's3:DeleteObject*',
                's3:PutObject',
                's3:PutObjectLegalHold',
                's3:PutObjectRetention',
                's3:PutObjectTagging',
                's3:PutObjectVersionTagging',
                's3:Abort*',
              ]),
              Effect: 'Allow',
            }),
          ]),
        },
      });
    });
  });

  describe('schedule', () => {
    test('creates an EventBridge rule for monthly trigger', () => {
      template.hasResourceProperties('AWS::Events::Rule', {
        Name: 'storm-tracking-monthly',
        ScheduleExpression: 'cron(0 3 1 * ? *)',
        State: 'DISABLED',
      });
    });

    test('EventBridge rule targets the state machine', () => {
      template.hasResourceProperties('AWS::Events::Rule', {
        Name: 'storm-tracking-monthly',
        Targets: Match.arrayWith([
          Match.objectLike({
            Input: Match.anyValue(),
          }),
        ]),
      });
    });
  });

  describe('alarms', () => {
    test('creates an SNS topic for alerts', () => {
      template.hasResourceProperties('AWS::SNS::Topic', {
        TopicName: 'storm-tracking-alerts',
      });
    });

    test('creates a pipeline failure alarm', () => {
      template.hasResourceProperties('AWS::CloudWatch::Alarm', {
        AlarmName: 'storm-tracking-pipeline-failure',
        Threshold: 1,
        EvaluationPeriods: 1,
        TreatMissingData: 'notBreaching',
      });
    });

    test('creates a cost alarm', () => {
      template.hasResourceProperties('AWS::CloudWatch::Alarm', {
        AlarmName: 'storm-tracking-cost',
        Threshold: 50,
        TreatMissingData: 'notBreaching',
      });
    });

    test('adds email subscription when alertEmail is provided', () => {
      const app = new cdk.App();
      const stack = new StormTrackingPipelineStack(app, 'AlertTestStack', {
        useDockerAssets: false,
        alertEmail: 'test@example.com',
      });
      const alertTemplate = Template.fromStack(stack);
      alertTemplate.hasResourceProperties('AWS::SNS::Subscription', {
        Protocol: 'email',
        Endpoint: 'test@example.com',
      });
    });
  });

  describe('outputs', () => {
    test('exports the bucket name', () => {
      template.hasOutput('BucketName', {});
    });

    test('exports the state machine ARN', () => {
      template.hasOutput('StateMachineArn', {});
    });

    test('exports the cluster ARN', () => {
      template.hasOutput('ClusterArn', {});
    });
  });
});
