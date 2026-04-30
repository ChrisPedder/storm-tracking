#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { CiStack } from '../lib/ci-stack';
import { StormTrackingPipelineStack } from '../lib/pipeline-stack';

const app = new cdk.App();

const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT || '978861143017',
  region: process.env.CDK_DEFAULT_REGION || 'eu-central-1',
};

const useDockerAssets = app.node.tryGetContext('useDockerAssets') !== 'false';

new CiStack(app, 'StormTrackingCi', {
  env,
  githubOrg: 'ChrisPedder',
  githubRepo: 'storm-tracking',
});

new StormTrackingPipelineStack(app, 'StormTrackingPipeline', {
  env,
  useDockerAssets,
});
