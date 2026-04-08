import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as ses from 'aws-cdk-lib/aws-ses';
import { parseDomain } from './lib/domain';

export interface SesStackProps extends cdk.StackProps {
  domainName: string;
  fromEmails: string[];
}

export class SesStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: SesStackProps) {
    super(scope, id, props);

    const { domainName, fromEmails } = props;
    const { zoneName } = parseDomain(domainName);
    const domainId = domainName.replace(/\./g, '-');

    const hostedZone = route53.HostedZone.fromLookup(this, `${zoneName}-hosted-zone`, {
      domainName: zoneName,
    });

    const domainIdentity = new ses.EmailIdentity(this, `${domainId}-ses-domain`, {
      identity: ses.Identity.publicHostedZone(hostedZone),
      dkimIdentity: ses.DkimIdentity.easyDkim(),
      mailFromDomain: `mail.${domainName}`,
    });

    for (const fromEmail of fromEmails) {
      const fromEmailId = fromEmail.replace(/[@.]/g, '-');
      const emailIdentity = new ses.EmailIdentity(this, `${fromEmailId}-ses-email`, {
        identity: ses.Identity.email(fromEmail),
      });

      emailIdentity.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
      new cdk.CfnOutput(this, `SesEmailIdentity-${fromEmailId}`, { value: fromEmail });
    }

    domainIdentity.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    new cdk.CfnOutput(this, 'SesDomainIdentity', { value: domainName });

    cdk.Annotations.of(this).addWarning(
      'SES sandbox: request production access in SES (us-east-1) to send to unverified recipients.'
    );
    cdk.Annotations.of(this).addWarning(
      `SES sandbox: verify recipient email(s) used for testing (${fromEmails.join(', ')}). AWS sends a verification email with a confirmation link.`
    );
  }
}
