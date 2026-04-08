import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ecr from 'aws-cdk-lib/aws-ecr';

// Default props
const defaultProps: Partial<ecr.RepositoryProps> = {
  removalPolicy: cdk.RemovalPolicy.DESTROY,
  lifecycleRules: [
    {
      description: 'Keep only 5 images',
      maxImageCount: 5,
      tagStatus: ecr.TagStatus.ANY,
    },
    {
      description: 'Delete untagged images older than 1 day',
      maxImageAge: cdk.Duration.days(1),
      tagStatus: ecr.TagStatus.UNTAGGED,
    },
  ],
};

export interface ECRStackProps extends cdk.StackProps {
  repositoryName: string;
  repositoryProps?: Partial<ecr.RepositoryProps>;
};

export class ECRStack extends cdk.Stack {
  public readonly repository: ecr.Repository;

  constructor(scope: Construct, id: string, props: ECRStackProps) {
    super(scope, id, props);

    const {
      repositoryName,
      repositoryProps = {}
    } = props;

    // Merge caller props with defaults
    const mergedProps: ecr.RepositoryProps = {
      ...defaultProps,
      ...repositoryProps,
      repositoryName,
    };

    this.repository = new ecr.Repository(this, `ecr-repository-${repositoryName}`, mergedProps);

    // Output the repository URI
    new cdk.CfnOutput(this, 'repository-uri', {
      value: this.repository.repositoryUri,
      description: 'URI of the ECR repository',
    });

    new cdk.CfnOutput(this, 'repository-arn', {
      value: this.repository.repositoryArn,
      description: 'ARN of the ECR repository',
    });
  }
}
