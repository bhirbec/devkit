import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';

export interface S3StackProps extends cdk.StackProps {
  bucketName?: string;
};

export class S3Stack extends cdk.Stack {
  public readonly bucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: S3StackProps) {
    super(scope, id, props);

    const { bucketName } = props;
    const defaultBucketName = 'prdhub-documents';

    this.bucket = new s3.Bucket(this, 'documents-bucket', {
      bucketName: bucketName || defaultBucketName,
      publicReadAccess: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      autoDeleteObjects: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      enforceSSL: true,
      versioned: true,
      lifecycleRules: [
        {
          id: 'delete-incomplete-multipart-uploads',
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
        },
      ],
    });

    // Output the bucket name and ARN
    new cdk.CfnOutput(this, 'bucket-name', {
      value: this.bucket.bucketName,
      description: 'Name of the S3 bucket for document storage',
    });

    new cdk.CfnOutput(this, 'bucket-arn', {
      value: this.bucket.bucketArn,
      description: 'ARN of the S3 bucket for document storage',
    });
  }

  /**
   * Grant read/write permissions to a Lambda function for this S3 bucket
   */
  public grantReadWriteToLambda(lambdaFunction: cdk.aws_lambda.Function): void {
    this.bucket.grantReadWrite(lambdaFunction);
  }

  /**
   * Grant read permissions to a Lambda function for this S3 bucket
   */
  public grantReadToLambda(lambdaFunction: cdk.aws_lambda.Function): void {
    this.bucket.grantRead(lambdaFunction);
  }
}
