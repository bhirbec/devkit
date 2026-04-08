import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as route53 from 'aws-cdk-lib/aws-route53';

export interface ClerkStackProps extends cdk.StackProps {
  zoneName: string;
  cnames: {
    name: string;   // e.g. 'clerk.prdhub.com'
    value: string;  // e.g. 'frontend-api.clerk.services'
  }[];
}

export class ClerkStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ClerkStackProps) {
    super(scope, id, props);

    const { zoneName, cnames } = props;

    const zone = route53.HostedZone.fromLookup(this, `${zoneName}-zone`, {
      domainName: zoneName
    });

    for (const { name, value } of cnames) {
      const record = new route53.CnameRecord(this, `CNAME-${name}`, {
        zone,
        recordName: name.replace(`.${zoneName}`, ''), // e.g. 'clerk'
        domainName: value,                            // e.g. 'frontend-api.clerk.services'
        ttl: cdk.Duration.minutes(5),
      });

      record.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

      new cdk.CfnOutput(this, `Output-${name}`, {
        value: `${name} → ${value}`,
      });
    }
  }
}
