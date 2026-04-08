import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apprunner from 'aws-cdk-lib/aws-apprunner';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import { PermissionArns, grantArnPermissions } from './lib/perms';

// Default props
const defaultAppRunnerProps: Partial<apprunner.CfnServiceProps> = {
  instanceConfiguration: {
    cpu: '1024',
    memory: '2048',
  },
};

export interface AppRunnerStackProps extends cdk.StackProps {
  ecrRepository: ecr.Repository;
  imageTag?: string;
  permissionArns: PermissionArns;
  environmentVariables?: { [key: string]: string };
}

export class AppRunnerStack extends cdk.Stack {
  public readonly service: apprunner.CfnService;

  constructor(scope: Construct, id: string, props: AppRunnerStackProps) {
    super(scope, id, props);

    const {
      ecrRepository,
      imageTag = 'latest',
      permissionArns,
      environmentVariables = {}
    } = props;

    // Create IAM role for App Runner service
    const appRunnerRole = new iam.Role(this, `app-runner:runner-role:${id}`, {
      assumedBy: new iam.ServicePrincipal('tasks.apprunner.amazonaws.com'),
    });

    // Create IAM role for App Runner to access ECR
    const accessRole = new iam.Role(this, `app-runner:access-role:${id}`, {
      assumedBy: new iam.ServicePrincipal('build.apprunner.amazonaws.com'),
    });

    // Grant permissions to explicit resource ARNs
    grantArnPermissions(appRunnerRole, permissionArns);

    // Grant ECR pull permissions
    ecrRepository.grantPull(accessRole);

    // Prepare environment variables for App Runner
    const appRunnerEnvVars = Object.entries(environmentVariables).map(([name, value]) => ({
      name,
      value,
    }));

    // Create App Runner service using CfnService
    this.service = new apprunner.CfnService(this, `app-runner:service:${id}`, {
      ...defaultAppRunnerProps,
      serviceName: `${id}-service`,
      sourceConfiguration: {
        imageRepository: {
          imageIdentifier: `${ecrRepository.repositoryUri}:${imageTag}`,
          imageConfiguration: {
            port: '8000',
            runtimeEnvironmentVariables: appRunnerEnvVars,
          },
          imageRepositoryType: 'ECR',
        },
        autoDeploymentsEnabled: true,
        authenticationConfiguration: {
          accessRoleArn: accessRole.roleArn,
        },
      },
      instanceConfiguration: {
        ...defaultAppRunnerProps.instanceConfiguration,
        instanceRoleArn: appRunnerRole.roleArn,
      },
    });

    // Apply removal policy
    this.service.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    // Output the service URL
    new cdk.CfnOutput(this, `app-runner:service-url:${id}`, {
      value: this.service.attrServiceUrl,
      description: 'URL of the App Runner service',
    });

    new cdk.CfnOutput(this, `app-runner:service-info:${id}`, {
      value: 'App Runner service created successfully. Configure custom domain manually in AWS Console if needed.',
      description: 'Information about the App Runner service',
    });
  }
}
