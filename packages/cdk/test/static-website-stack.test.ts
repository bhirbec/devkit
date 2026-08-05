import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { StaticWebsiteStack, StaticWebsiteStackProps } from '../src/static-website-stack';

const env = { account: '111111111111', region: 'us-east-1' };

const synth = (id: string, props: Omit<StaticWebsiteStackProps, 'env'>): Template => {
  const app = new cdk.App();
  return Template.fromStack(new StaticWebsiteStack(app, id, { env, ...props }));
};

describe('without a domain name', () => {
  const template = synth('trainer-site', {});

  test('creates the bucket and the distribution', () => {
    template.resourceCountIs('AWS::S3::Bucket', 1);
    template.resourceCountIs('AWS::CloudFront::Distribution', 1);
  });

  test('creates no Route 53 or ACM resources', () => {
    template.resourceCountIs('AWS::Route53::RecordSet', 0);
    template.resourceCountIs('AWS::CertificateManager::Certificate', 0);
  });

  test('serves on the CloudFront default domain', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        Aliases: Match.absent(),
        ViewerCertificate: Match.absent(),
        DefaultRootObject: 'index.html',
      }),
    });
  });

  test('falls back to the stack id for the default bucket name', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: 'trainer-site-static-website',
    });
  });

  test('outputs the generated distribution domain name', () => {
    const outputs = template.findOutputs('*');
    expect(Object.keys(outputs).sort()).toEqual(['distributiondomainname', 'distributionid']);
  });

  test('an explicit bucketName still wins', () => {
    const t = synth('trainer-site', { bucketName: 'my-bucket' });
    t.hasResourceProperties('AWS::S3::Bucket', { BucketName: 'my-bucket' });
  });
});

describe('with a domain name', () => {
  const template = synth('landing-page', { domainName: 'example.com' });

  test('keeps the alias record and the certificate', () => {
    template.resourceCountIs('AWS::Route53::RecordSet', 1);
    template.resourceCountIs('AWS::CertificateManager::Certificate', 1);
  });

  test('attaches the domain to the distribution', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({ Aliases: ['example.com'] }),
    });
  });

  test('derives the default bucket name from the domain, not the stack id', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: 'example.com-static-website',
    });
  });

  test('emits only the pre-existing output', () => {
    expect(Object.keys(template.findOutputs('*'))).toEqual(['distributionid']);
  });

  test('a subdomain targets the parent hosted zone', () => {
    const t = synth('app', { domainName: 'app.example.com' });
    t.hasResourceProperties('AWS::Route53::RecordSet', {
      Name: 'app.example.com.',
    });
  });
});

describe('with an /api/* lambda', () => {
  const apiProps = {
    functionProps: {
      functionName: 'trainer-api',
      runtime: lambda.Runtime.PYTHON_3_13,
    },
    environmentVariables: { STAGE: 'prod' },
    permissionArns: {
      dynamodb: { write: ['arn:aws:dynamodb:us-east-1:111111111111:table/Sessions'] },
    },
  };

  const template = synth('trainer-site', { api: apiProps });

  test('creates the lambda with its environment', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'trainer-api',
      Runtime: 'python3.13',
      Environment: { Variables: { STAGE: 'prod' } },
    });
  });

  test('grants the requested permissions', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['dynamodb:PutItem']),
            Resource: Match.arrayWith(['arn:aws:dynamodb:us-east-1:111111111111:table/Sessions']),
          }),
        ]),
      }),
    });
  });

  test('exposes the lambda through an IAM-signed function URL only', () => {
    template.hasResourceProperties('AWS::Lambda::Url', { AuthType: 'AWS_IAM' });
    template.hasResourceProperties('AWS::CloudFront::OriginAccessControl', {
      OriginAccessControlConfig: Match.objectLike({
        OriginAccessControlOriginType: 'lambda',
        SigningProtocol: 'sigv4',
      }),
    });
    template.hasResourceProperties('AWS::Lambda::Permission', {
      Action: 'lambda:InvokeFunctionUrl',
      Principal: 'cloudfront.amazonaws.com',
    });
  });

  test('routes /api/* on the website distribution, uncached', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        CacheBehaviors: [
          Match.objectLike({
            PathPattern: '/api/*',
            // 4135ea2d-6df8-44a3-9df3-4b5a84be39ad = CachingDisabled
            CachePolicyId: '4135ea2d-6df8-44a3-9df3-4b5a84be39ad',
            // b689b0a8-53d0-40ab-baf2-68738e2966ac = AllViewerExceptHostHeader
            OriginRequestPolicyId: 'b689b0a8-53d0-40ab-baf2-68738e2966ac',
          }),
        ],
      }),
    });
  });

  test('does not run the .html rewrite function on API requests', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        CacheBehaviors: [Match.objectLike({ FunctionAssociations: Match.absent() })],
        DefaultCacheBehavior: Match.objectLike({
          FunctionAssociations: Match.anyValue(),
        }),
      }),
    });
  });

  test('honours a custom path pattern', () => {
    const t = synth('trainer-site', { api: { ...apiProps, pathPattern: '/backend/*' } });
    t.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        CacheBehaviors: [Match.objectLike({ PathPattern: '/backend/*' })],
      }),
    });
  });

  test('works alongside a custom domain', () => {
    const t = synth('trainer-site', { domainName: 'example.com', api: apiProps });
    t.resourceCountIs('AWS::Route53::RecordSet', 1);
    t.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        Aliases: ['example.com'],
        CacheBehaviors: [Match.objectLike({ PathPattern: '/api/*' })],
      }),
    });
  });
});

describe('without an api', () => {
  test('creates no lambda and no extra behavior', () => {
    const template = synth('trainer-site', {});
    template.resourceCountIs('AWS::Lambda::Url', 0);
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({ CacheBehaviors: Match.absent() }),
    });
  });
});
