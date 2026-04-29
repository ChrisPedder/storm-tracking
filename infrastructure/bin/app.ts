#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { StormTrackingPipelineStack } from '../lib/pipeline-stack';

const app = new cdk.App();
new StormTrackingPipelineStack(app, 'StormTrackingPipeline');
