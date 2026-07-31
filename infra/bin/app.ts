#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { CareerCopilotStack } from "../lib/career-copilot-stack";
import { MonitoringStack } from "../lib/monitoring-stack";

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION || "us-east-1",
};

const main = new CareerCopilotStack(app, "career-copilot", {
  env,
  description: "career-copilot v2: daily ATS sweep, worklist read API, briefing store",
});

const monitoring = new MonitoringStack(app, "career-copilot-monitoring", {
  env,
  description: "Alarms for career-copilot — including a fetch that returns implausibly few roles",
  // Plain strings, never constructs: see the note in monitoring-stack.ts on why
  // this stack must not hold a CloudFormation Export from the app stack.
  refs: main.refs,
});

// The only real ordering constraint: the cron's log group is created by the app
// stack, and the monitoring stack attaches metric filters to it by name. Declared
// as a stack dependency rather than by passing the construct, so `cdk deploy
// --all` orders them correctly without creating an Export that would then pin the
// app stack's resources for as long as the alarms reference them.
monitoring.addDependency(main);
