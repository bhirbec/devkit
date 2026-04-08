import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as cloudfront_origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import { parseDomain } from './lib/domain';

const DefaultNotFound: string = "/404.html";

export interface StaticWebsiteStackProps extends cdk.StackProps {
  domainName: string;
  bucketName?: string;
  notFoundPage?: string;
  env?: cdk.Environment;
};

export class StaticWebsiteStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: StaticWebsiteStackProps) {
    super(scope, id, props);

    const { bucketName, domainName } = props;
    const { zoneName, subDomainName } = parseDomain(domainName);

    const zone = route53.HostedZone.fromLookup(this, `${zoneName}-zone`, {
      domainName: zoneName
    });

    const cert = new acm.Certificate(this, `${domainName}-certificate`, {
      domainName: domainName,
      validation: acm.CertificateValidation.fromDns(zone),
    });

    const defaultBucketName = `${domainName}-static-website`;

    const bucket = new s3.Bucket(this, `${domainName}-bucket`, {
      bucketName: bucketName || defaultBucketName,
      publicReadAccess: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      autoDeleteObjects: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      enforceSSL: true,
    });

    // Rewrite Function
    const rewriteFunction = new cloudfront.Function(this, 'RewriteToHtmlFunction', {
      code: cloudfront.FunctionCode.fromInline(`
        function handler(event) {
          var request = event.request;
          var uri = request.uri;

          var lastSegment = uri.split('/').pop();

          if (uri.endsWith('/')) {
            request.uri += 'index.html';
          } else if (!lastSegment.includes('.')) {
            request.uri += '.html';
          }

          return request;
        }
      `)
    });

    const distribution = new cloudfront.Distribution(this, `${domainName}-distribution`, {
      defaultBehavior: {
        origin: cloudfront_origins.S3BucketOrigin.withOriginAccessControl(bucket, {
          originAccessLevels: [cloudfront.AccessLevel.READ],
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        compress: true,
        functionAssociations: [
          {
            eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
            function: rewriteFunction,
          },
        ],
      },
      domainNames: [domainName],
      defaultRootObject: 'index.html',
      certificate: cert,
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.minutes(1),
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: props.notFoundPage || DefaultNotFound,
          ttl: cdk.Duration.minutes(1),
        },
      ],
    });

    bucket.addToResourcePolicy(new iam.PolicyStatement({
      sid: `${domainName}-s3-access-for-cloudfront`,
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject'],
      resources: [`${bucket.bucketArn}/*`],
      principals: [new iam.ServicePrincipal('cloudfront.amazonaws.com')],
      conditions: {
        StringEquals: {
          'AWS:SourceArn': `arn:aws:cloudfront::${this.account}:distribution/${distribution.distributionId}`,
        },
      },
    }));

    new route53.ARecord(this, `${domainName}-alias`, {
      zone,
      recordName: subDomainName,
      target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(distribution)),
    });

    cert.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    distribution.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    new cdk.CfnOutput(this, 'distribution-id', {
      value: distribution.distributionId,
    });
  }
};
