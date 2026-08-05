# Shared CDK Library

## Commands

```bash
cd packages/cdk
yarn install
yarn build
yarn test
```

## StaticWebsiteStack

S3 + CloudFront. `domainName` is optional.

With a domain, the stack also looks up the Route 53 hosted zone, issues a
DNS-validated ACM certificate and creates the alias record:

```ts
new StaticWebsiteStack(app, 'landing-page', { env, domainName: 'example.com' });
```

Without one, the site is served on CloudFront's generated
`d<random>.cloudfront.net` name — no hosted zone, no certificate, no Route 53
record — and that name is exported as the `distributiondomainname` output:

```ts
new StaticWebsiteStack(app, 'trainer-site', { env });
```

The default bucket name and the construct ids are derived from `domainName`
when it is given, and from the stack id otherwise.

### Serving an API from the same distribution

Pass `api` to run a Lambda behind an `/api/*` behavior on the website's own
distribution. The browser stays same-origin, so no CORS configuration is
needed. The Lambda is reached through a Function URL locked to `AWS_IAM` and
signed by a CloudFront origin access control, so it is not publicly callable.

```ts
new StaticWebsiteStack(app, 'trainer-site', {
  env,
  api: {
    functionProps: { functionName: 'trainer-api', runtime: lambda.Runtime.PYTHON_3_13 },
    environmentVariables: { STAGE: 'prod' },
    permissionArns: { dynamodb: { write: [sessionsTableArn] } },
    // pathPattern defaults to '/api/*'
  },
});
```

`LambdaApiStack` is unchanged and still the right choice when the API needs its
own subdomain (`api.<domain>`) behind API Gateway.
