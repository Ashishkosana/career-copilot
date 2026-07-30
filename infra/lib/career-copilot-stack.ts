import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigw from "aws-cdk-lib/aws-apigateway";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as secrets from "aws-cdk-lib/aws-secretsmanager";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as path from "path";

export class CareerCopilotStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Single-table store: briefings, jobs (dedup), pipeline.
    const table = new dynamodb.Table(this, "Table", {
      tableName: "career-copilot",
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Third-party keys live in Secrets Manager (the Crewtron pattern), seeded
    // out-of-band by scripts/seed-secrets.sh. Never in code or git.
    const gmailSecret = new secrets.Secret(this, "GmailSecret", {
      secretName: "career-copilot/gmail",
      description: "Gmail OAuth credentials.json + token.json for the agent",
    });
    const claudeSecret = new secrets.Secret(this, "ClaudeSecret", {
      secretName: "career-copilot/anthropic",
      description: "Anthropic (Claude) API key",
    });
    const apifySecret = new secrets.Secret(this, "ApifySecret", {
      secretName: "career-copilot/apify",
      description: "Apify personal API token",
    });

    // Lambda code is pre-built (Docker-free) by infra/build-lambda.sh, which
    // pip-installs the deps as manylinux wheels + copies career_copilot into
    // infra/build. Run that script before `cdk deploy`.
    const code = lambda.Code.fromAsset(path.join(__dirname, "..", "build"));
    const common = {
      runtime: lambda.Runtime.PYTHON_3_13,
      code,
      environment: {
        TABLE_NAME: table.tableName,
        GMAIL_SECRET_ID: gmailSecret.secretName,
      },
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
    };

    // Cognito — the Crewtron auth pattern. The mobile app signs in here and
    // sends the ID token; API Gateway's authorizer verifies it and injects the
    // `sub` claim that becomes our DynamoDB userId.
    const userPool = new cognito.UserPool(this, "UserPool", {
      userPoolName: "career-copilot",
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: { email: { required: true, mutable: true } },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const userPoolClient = userPool.addClient("MobileClient", {
      authFlows: { userSrp: true },
    });

    // OWNER_USER_ID = your Cognito `sub`; the cron writes the briefing under it
    // so the app (same user) reads it back. Fill after creating your user.
    const ownerUserId = this.node.tryGetContext("ownerUserId") || "";

    // Daily cron: fetch inbox -> triage -> jobs -> store briefing -> email.
    const cronFn = new lambda.Function(this, "CronFn", {
      ...common,
      handler: "career_copilot.lambda_handler.cron_handler",
      environment: {
        ...common.environment,
        // Public repo: never hardcode a personal address here. Supply it at
        // deploy time, e.g. `cdk deploy -c myEmail=you@example.com`.
        MY_EMAIL: this.node.tryGetContext("myEmail") ?? process.env.MY_EMAIL ?? "",
        OWNER_USER_ID: ownerUserId,
        CLAUDE_SECRET_ID: claudeSecret.secretName,
        APIFY_SECRET_ID: apifySecret.secretName,
      },
      timeout: cdk.Duration.seconds(120),
    });
    table.grantReadWriteData(cronFn);
    gmailSecret.grantRead(cronFn);
    claudeSecret.grantRead(cronFn);
    apifySecret.grantRead(cronFn);

    new events.Rule(this, "DailyRule", {
      // 07:00 America/Phoenix = 14:00 UTC (AZ has no DST).
      schedule: events.Schedule.cron({ minute: "0", hour: "14" }),
      targets: [new targets.LambdaFunction(cronFn)],
    });

    // API: GET /briefing -> latest stored briefing (for the Flutter app).
    const apiFn = new lambda.Function(this, "ApiFn", {
      ...common,
      handler: "career_copilot.lambda_handler.api_handler",
    });
    table.grantReadData(apiFn);

    const api = new apigw.RestApi(this, "Api", {
      restApiName: "career-copilot",
      defaultCorsPreflightOptions: {
        allowOrigins: apigw.Cors.ALL_ORIGINS,
        allowMethods: apigw.Cors.ALL_METHODS,
      },
    });
    const authorizer = new apigw.CognitoUserPoolsAuthorizer(this, "Authorizer", {
      cognitoUserPools: [userPool],
    });
    api.root
      .addResource("briefing")
      .addMethod("GET", new apigw.LambdaIntegration(apiFn), {
        authorizer,
        authorizationType: apigw.AuthorizationType.COGNITO,
      });

    new cdk.CfnOutput(this, "ApiUrl", { value: api.url });
    new cdk.CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
    new cdk.CfnOutput(this, "UserPoolClientId", {
      value: userPoolClient.userPoolClientId,
    });
    new cdk.CfnOutput(this, "GmailSecretName", { value: gmailSecret.secretName });
  }
}
