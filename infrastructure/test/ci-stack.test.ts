import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { CiStack } from '../lib/ci-stack';

describe('CiStack', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new CiStack(app, 'TestCi', {
      githubOrg: 'TestOrg',
      githubRepo: 'test-repo',
    });
    template = Template.fromStack(stack);
  });

  test('creates a deploy IAM role named github-actions-deploy', () => {
    template.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'github-actions-deploy',
    });
  });

  test('deploy role has AdministratorAccess', () => {
    template.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'github-actions-deploy',
      ManagedPolicyArns: Match.arrayWith([
        Match.objectLike({
          'Fn::Join': Match.arrayWith([
            Match.arrayWith([
              Match.stringLikeRegexp('AdministratorAccess'),
            ]),
          ]),
        }),
      ]),
    });
  });

  test('deploy role has max session duration of 1 hour', () => {
    template.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'github-actions-deploy',
      MaxSessionDuration: 3600,
    });
  });

  test('outputs the deploy role ARN', () => {
    template.hasOutput('DeployRoleArn', {});
  });
});
