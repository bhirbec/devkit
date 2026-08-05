import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as cloudfront_origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import { parseDomain } from './lib/domain';
import { LambdaFunctionProps, createLambdaFunction } from './lib/lambda';
import { PermissionArns, grantArnPermissions } from './lib/perms';

const DefaultNotFound: string = "/404.html";
const DefaultApiPathPattern: string = "/api/*";

/**
 * Serve a Lambda from the website's own CloudFront distribution, under a path
 * prefix. The browser stays same-origin, so no CORS setup is needed.
 */
export interface StaticWebsiteApiProps {
  functionProps: LambdaFunctionProps;
  permissionArns?: PermissionArns;
  environmentVariables?: { [key: string]: string };
  /** CloudFront path pattern routed to the Lambda. Defaults to `/api/*`. */
  pathPattern?: string;
};

export interface StaticWebsiteStackProps extends cdk.StackProps {
  /**
   * Custom domain to serve the site on. When omitted, the site is served on
   * CloudFront's default `*.cloudfront.net` name and no Route 53 hosted zone,
   * ACM certificate or alias record is created.
   */
  domainName?: string;
  bucketName?: string;
  notFoundPage?: string;
  api?: StaticWebsiteApiProps;
  env?: cdk.Environment;
};

export class StaticWebsiteStack extends cdk.Stack {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;
  public readonly apiFunction?: lambda.Function;

  constructor(scope: Construct, id: string, props: StaticWebsiteStackProps) {
    super(scope, id, props);

    const { bucketName, domainName, api } = props;

    // Construct ids and the default bucket name are derived from the custom
    // domain. Without one we fall back to the stack id: renaming a construct id
    // replaces the resource it maps to, so this must stay `domainName` whenever
    // a domain is given.
    const nameBase = domainName || id;

    let zone: route53.IHostedZone | undefined;
    let cert: acm.Certificate | undefined;
    let subDomainName: string = "";

    if (domainName) {
      const parsed = parseDomain(domainName);
      subDomainName = parsed.subDomainName;

      zone = route53.HostedZone.fromLookup(this, `${parsed.zoneName}-zone`, {
        domainName: parsed.zoneName
      });

      cert = new acm.Certificate(this, `${domainName}-certificate`, {
        domainName: domainName,
        validation: acm.CertificateValidation.fromDns(zone),
      });
    }

    const defaultBucketName = `${nameBase}-static-website`;

    const bucket = new s3.Bucket(this, `${nameBase}-bucket`, {
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

    let apiFunction: lambda.Function | undefined;
    let additionalBehaviors: Record<string, cloudfront.BehaviorOptions> | undefined;

    if (api) {
      apiFunction = createLambdaFunction(this, api.functionProps);

      Object.entries(api.environmentVariables || {}).forEach(([name, value]) => {
        apiFunction!.addEnvironment(name, value);
      });

      if (api.permissionArns) {
        grantArnPermissions(apiFunction, api.permissionArns);
      }

      // A Function URL locked to AWS_IAM: only the distribution's origin access
      // control can reach it, so the Lambda is not publicly callable.
      const functionUrl = apiFunction.addFunctionUrl({
        authType: lambda.FunctionUrlAuthType.AWS_IAM,
      });

      additionalBehaviors = {
        // No rewrite function here on purpose: appending `.html` to API paths
        // would break them.
        [api.pathPattern || DefaultApiPathPattern]: {
          origin: cloudfront_origins.FunctionUrlOrigin.withOriginAccessControl(functionUrl),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          // The Host header must not be forwarded: SigV4 signs the origin host.
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        },
      };
    }

    const distribution = new cloudfront.Distribution(this, `${nameBase}-distribution`, {
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
      // Spread rather than pass `undefined`: CDK's analytics metadata records
      // every key present on the props object, so an always-present key would
      // change the template of stacks that do not use it.
      ...(additionalBehaviors ? { additionalBehaviors } : {}),
      domainNames: domainName ? [domainName] : undefined,
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
      sid: `${nameBase}-s3-access-for-cloudfront`,
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

    if (domainName && zone) {
      new route53.ARecord(this, `${domainName}-alias`, {
        zone,
        recordName: subDomainName,
        target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(distribution)),
      });
    }

    cert?.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    distribution.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    new cdk.CfnOutput(this, 'distribution-id', {
      value: distribution.distributionId,
    });

    if (!domainName) {
      // Only emitted without a custom domain: adding an output to the existing
      // domain-backed stacks would change their template.
      new cdk.CfnOutput(this, 'distribution-domain-name', {
        value: distribution.distributionDomainName,
      });
    }

    this.bucket = bucket;
    this.distribution = distribution;
    this.apiFunction = apiFunction;
  }
};
