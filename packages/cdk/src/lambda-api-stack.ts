import * as path from 'path';
import * as fs from 'fs';
import * as child_process from 'child_process';
import * as os from 'os';

import * as cdk from 'aws-cdk-lib';
import { Duration } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import { parseDomain } from './lib/domain';
import { PermissionArns, grantArnPermissions } from './lib/perms';

type RequiredFunctionProps = Required<Pick<lambda.FunctionProps, 'functionName' | 'runtime'>>;

export interface LambdaApiStackProps extends cdk.StackProps {
  functionProps: RequiredFunctionProps & Partial<lambda.FunctionProps>;
  domainName: string;
  permissionArns: PermissionArns;
  environmentVariables?: { [key: string]: string };
};

export class LambdaApiStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: LambdaApiStackProps) {
    super(scope, id, props);

    const { domainName, permissionArns, functionProps, environmentVariables = {} } = props;
    const { zoneName, subDomainName } = parseDomain(domainName);

    // define the Lambda
    const isPython =
      functionProps.runtime.name === lambda.Runtime.PYTHON_3_11.name ||
      functionProps.runtime.name === lambda.Runtime.PYTHON_3_13.name;

    let lambdaFunction: lambda.Function;
    if (isPython) {
      lambdaFunction = this.createPythonLambda(functionProps);
    } else if (functionProps.runtime.name === lambda.Runtime.PROVIDED_AL2.name) {
      lambdaFunction = this.createGoLambda(functionProps);
    } else {
      throw new Error(`Unsupported runtime: ${functionProps.runtime.name}`);
    }

    // Add environment variables
    Object.entries(environmentVariables).forEach(([name, value]) => {
      lambdaFunction.addEnvironment(name, value);
    });

    // Grant permissions to explicit resource ARNs
    grantArnPermissions(lambdaFunction, permissionArns);

    // Hosted zone for domain
    const hostedZone = route53.HostedZone.fromLookup(this, `${zoneName}-hosted-zone`, {
      domainName: zoneName,
    });

    // Create a certificate for the custom domain
    const certificate = new acm.Certificate(this, `${domainName}-certificate`, {
      domainName: domainName,
      validation: acm.CertificateValidation.fromDns(hostedZone),
    });

    // Define the API Gateway (EDGE optimized by default)
    const api = new apigateway.LambdaRestApi(this, `${domainName}-apigateway`, {
      handler: lambdaFunction,
      domainName: {
        domainName: domainName,
        certificate: certificate,
      },
    });

    // Create a DNS record to point to the API Gateway
    const rect = new route53.ARecord(this, `${domainName}-record`, {
      zone: hostedZone,
      recordName: subDomainName,
      target: route53.RecordTarget.fromAlias(new route53Targets.ApiGateway(api)),
    });

    // Careful, this is *very* permissive
    api.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    lambdaFunction.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    certificate.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    rect.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    // Output the API endpoint
    new cdk.CfnOutput(this, 'api URL', { value: api.url });
  }

  private createPythonLambda(props: RequiredFunctionProps & Partial<lambda.FunctionProps>): lambda.Function {
    const defaultProps: Pick<lambda.FunctionProps, 'runtime' | 'code' | 'timeout' | 'handler'> = {
      runtime: lambda.Runtime.PYTHON_3_13,
      code: lambda.Code.fromInline(" "),
      timeout: Duration.seconds(30),
      handler: "lambda.main",
    };

    const lambdaFunction = new lambda.Function(this, props.functionName, {
      ...defaultProps,
      ...props,
    });

    return lambdaFunction;
  }

  private createGoLambda(props: RequiredFunctionProps & Partial<lambda.FunctionProps>): lambda.Function {
    // Golang hanler must be named "bootstrap"
    const handler = 'bootstrap';

    // Create and zip the dummy Go Lambda function
    const tempDir = path.join(os.tmpdir(), 'dummy-go');
    const zipFilePath = this.createDummyGoPackage(tempDir, handler);

    const defaultProps: Pick<lambda.FunctionProps, 'runtime' | 'code' | 'timeout' | 'handler'> = {
      runtime: lambda.Runtime.PROVIDED_AL2,
      code: lambda.Code.fromAsset(zipFilePath),
      timeout: Duration.seconds(30),
      handler: handler,
    };

    const mergedPropsprops: lambda.FunctionProps = {
      ...defaultProps,
      ...props,
    };

    const lambdaFunction = new lambda.Function(this, props.functionName, mergedPropsprops);

    fs.rmSync(tempDir, { recursive: true, force: true });

    return lambdaFunction;
  }
  private createDummyGoPackage(tempDir: string, handlerName: string): string {
    // Create the temporary directory
    if (fs.existsSync(tempDir)) {
      fs.rmdirSync(tempDir, { recursive: true });
    }
    fs.mkdirSync(tempDir);

    // Create a dummy Go file
    const gopath = path.join(tempDir, 'main.go');
    fs.writeFileSync(gopath, "package main; func main() {}");

    // Compile the Go binary and zip it in one step
    const zippath = path.join(tempDir, 'function.zip');
    child_process.execSync(`GOOS=linux GOARCH=amd64 go build -o ${handlerName} ${gopath} && zip -j ${zippath} ${handlerName}`, { cwd: tempDir });

    return zippath;
  }
};
