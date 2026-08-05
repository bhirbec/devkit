import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { LambdaApiStack } from '../src/lambda-api-stack';

const env = { account: '111111111111', region: 'us-east-1' };

// The Lambda packaging moved to lib/lambda.ts; this stack must keep behaving
// exactly as before, since it backs a live API.
describe('LambdaApiStack', () => {
  const app = new cdk.App();
  const template = Template.fromStack(new LambdaApiStack(app, 'api', {
    env,
    functionProps: {
      functionName: 'api',
      handler: 'lambda.main',
      runtime: lambda.Runtime.PYTHON_3_13,
    },
    domainName: 'api.example.com',
    permissionArns: {
      dynamodb: { write: ['arn:aws:dynamodb:us-east-1:111111111111:table/Users'] },
    },
    environmentVariables: { DOCUMENT_BUCKET: 'docs' },
  }));

  test('creates the lambda with the python defaults', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'api',
      Handler: 'lambda.main',
      Runtime: 'python3.13',
      Timeout: 30,
      Environment: { Variables: { DOCUMENT_BUCKET: 'docs' } },
    });
  });

  test('still fronts the api with API Gateway on its own subdomain', () => {
    template.resourceCountIs('AWS::ApiGateway::RestApi', 1);
    template.hasResourceProperties('AWS::ApiGateway::DomainName', {
      DomainName: 'api.example.com',
    });
    template.hasResourceProperties('AWS::Route53::RecordSet', {
      Name: 'api.example.com.',
    });
    template.resourceCountIs('AWS::CertificateManager::Certificate', 1);
  });

  test('grants the requested permissions', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Resource: Match.arrayWith(['arn:aws:dynamodb:us-east-1:111111111111:table/Users']),
          }),
        ]),
      }),
    });
  });

  test('rejects an unsupported runtime', () => {
    expect(() => new LambdaApiStack(new cdk.App(), 'bad', {
      env,
      functionProps: { functionName: 'api', runtime: lambda.Runtime.NODEJS_20_X },
      domainName: 'api.example.com',
      permissionArns: {},
    })).toThrow(/Unsupported runtime/);
  });
});
