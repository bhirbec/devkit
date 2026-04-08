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
export declare function grantArnPermissions(principal: iam.IGrantable, permissionArns: PermissionArns): void;
