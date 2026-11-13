import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ecr from 'aws-cdk-lib/aws-ecr';

// Default props
const defaultLifecycleRules: ecr.LifecycleRule[] = [
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
];

const defaultProps: Partial<ecr.RepositoryProps> = {
  removalPolicy: cdk.RemovalPolicy.DESTROY,
  lifecycleRules: defaultLifecycleRules,
};

export interface ECRStackProps extends cdk.StackProps {
  repositoryNames: string[];
  repositoryProps?: Partial<ecr.RepositoryProps>;
};

export class ECRStack extends cdk.Stack {
  public readonly repositories: Record<string, ecr.Repository> = {};

  constructor(scope: Construct, id: string, props: ECRStackProps) {
    super(scope, id, props);

    const { repositoryNames, repositoryProps = {} } = props;

    const mergedProps: Partial<ecr.RepositoryProps> = {
      ...defaultProps,
      ...repositoryProps,
    };

    for (const name of repositoryNames) {
      const repo = new ecr.Repository(this, `ecr-repository-${name}`, {
        ...mergedProps,
        repositoryName: name,
      });

      this.repositories[name] = repo;

      new cdk.CfnOutput(this, `${name}-uri`, {
        value: repo.repositoryUri,
        description: `URI of the ${name} ECR repository`,
      });

      new cdk.CfnOutput(this, `${name}-arn`, {
        value: repo.repositoryArn,
        description: `ARN of the ${name} ECR repository`,
      });
    }
  }
}
