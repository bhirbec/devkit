import * as iam from 'aws-cdk-lib/aws-iam';

export type PermissionArns = {
  dynamodb?: {
    read?: string[];
    write?: string[];
  };
  s3?: {
    read?: string[];
    write?: string[];
  };
};

const dynamodbReadActions = [
  'dynamodb:GetItem',
  'dynamodb:Query',
  'dynamodb:Scan',
  'dynamodb:BatchGetItem',
  'dynamodb:DescribeTable',
];

const dynamodbWriteActions = [
  ...dynamodbReadActions,
  'dynamodb:PutItem',
  'dynamodb:UpdateItem',
  'dynamodb:DeleteItem',
  'dynamodb:BatchWriteItem',
];

const s3ReadActions = [
  's3:GetObject',
];

const s3WriteActions = [
  ...s3ReadActions,
  's3:PutObject',
  's3:DeleteObject',
];

const s3ListBucketActions = [
  's3:ListBucket',
];

const uniqueStrings = (values: string[]): string[] => Array.from(new Set(values));

const dynamodbIndexArn = (tableArn: string): string => `${tableArn}/index/*`;

const normalizeBucketArn = (bucketArn: string): string => {
  if (bucketArn.endsWith('/*')) {
    return bucketArn.slice(0, -2);
  }

  return bucketArn;
};

const bucketObjectArn = (bucketArn: string): string => `${bucketArn}/*`;

const addPolicyStatement = (
  principal: iam.IGrantable,
  actions: string[],
  resources: string[]
): void => {
  if (resources.length === 0) {
    return;
  }

  principal.grantPrincipal.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions,
      resources,
    })
  );
};

export function grantArnPermissions(
  principal: iam.IGrantable,
  permissionArns: PermissionArns
): void {
  const dynamodbReadArns = uniqueStrings(permissionArns.dynamodb?.read ?? []);
  const dynamodbWriteArns = uniqueStrings(permissionArns.dynamodb?.write ?? []);
  const dynamodbReadIndexArns = uniqueStrings(dynamodbReadArns.map(dynamodbIndexArn));
  const dynamodbWriteIndexArns = uniqueStrings(dynamodbWriteArns.map(dynamodbIndexArn));
  const s3ReadBuckets = uniqueStrings((permissionArns.s3?.read ?? []).map(normalizeBucketArn));
  const s3WriteBuckets = uniqueStrings((permissionArns.s3?.write ?? []).map(normalizeBucketArn));

  addPolicyStatement(principal, dynamodbReadActions, uniqueStrings([...dynamodbReadArns, ...dynamodbReadIndexArns]));
  addPolicyStatement(principal, dynamodbWriteActions, uniqueStrings([...dynamodbWriteArns, ...dynamodbWriteIndexArns]));

  const s3ReadObjectArns = uniqueStrings(s3ReadBuckets.map(bucketObjectArn));
  const s3WriteObjectArns = uniqueStrings(s3WriteBuckets.map(bucketObjectArn));

  addPolicyStatement(principal, s3ListBucketActions, s3ReadBuckets);
  addPolicyStatement(principal, s3ReadActions, s3ReadObjectArns);

  addPolicyStatement(principal, s3ListBucketActions, s3WriteBuckets);
  addPolicyStatement(principal, s3WriteActions, s3WriteObjectArns);
}
