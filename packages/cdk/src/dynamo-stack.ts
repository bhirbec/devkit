import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';

// Default props
const defaultProps: Partial<dynamodb.TableProps> = {
  partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  removalPolicy: cdk.RemovalPolicy.DESTROY,
};

// Define Props
export interface DynamoStackProps extends cdk.StackProps {
  tableProps: dynamodb.TableProps;
  globalSecondaryIndexes?: dynamodb.GlobalSecondaryIndexProps[];
}

// Define Dynamo Stack
export class DynamoStack extends cdk.Stack {
  public readonly table: dynamodb.Table;

  constructor(scope: Construct, id: string, props: DynamoStackProps) {
    super(scope, id, props);

    // Merge caller props with defaults
    const mergedProps: dynamodb.TableProps = {
      ...defaultProps,
      ...props.tableProps,
    };

    // Create the table
    this.table = new dynamodb.Table(this, `${id}-table`, mergedProps);

    // Add GSIs if provided
    props.globalSecondaryIndexes?.forEach((gsi) => {
      this.table.addGlobalSecondaryIndex(gsi);
    });

    // Output table ARN
    new cdk.CfnOutput(this, 'TableArn', {
      value: this.table.tableArn,
    });
  }
}
