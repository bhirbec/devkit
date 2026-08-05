import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import { parseDomain } from './lib/domain';
import { LambdaFunctionProps, createLambdaFunction } from './lib/lambda';
import { PermissionArns, grantArnPermissions } from './lib/perms';

export interface LambdaApiStackProps extends cdk.StackProps {
  functionProps: LambdaFunctionProps;
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
    const lambdaFunction = createLambdaFunction(this, functionProps);

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
};
