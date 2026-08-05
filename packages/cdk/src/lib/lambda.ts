import * as path from 'path';
import * as fs from 'fs';
import * as child_process from 'child_process';
import * as os from 'os';

import { Duration } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';

export type RequiredFunctionProps = Required<Pick<lambda.FunctionProps, 'functionName' | 'runtime'>>;

export type LambdaFunctionProps = RequiredFunctionProps & Partial<lambda.FunctionProps>;

/**
 * Create a Lambda function, filling in sensible per-runtime defaults. Python
 * runtimes get a placeholder inline handler, Go gets a compiled dummy
 * `bootstrap` binary; both are meant to be replaced by a later code deploy.
 */
export function createLambdaFunction(scope: Construct, props: LambdaFunctionProps): lambda.Function {
  const isPython =
    props.runtime.name === lambda.Runtime.PYTHON_3_11.name ||
    props.runtime.name === lambda.Runtime.PYTHON_3_13.name;

  if (isPython) {
    return createPythonLambda(scope, props);
  }

  if (props.runtime.name === lambda.Runtime.PROVIDED_AL2.name) {
    return createGoLambda(scope, props);
  }

  throw new Error(`Unsupported runtime: ${props.runtime.name}`);
}

function createPythonLambda(scope: Construct, props: LambdaFunctionProps): lambda.Function {
  const defaultProps: Pick<lambda.FunctionProps, 'runtime' | 'code' | 'timeout' | 'handler'> = {
    runtime: lambda.Runtime.PYTHON_3_13,
    code: lambda.Code.fromInline(" "),
    timeout: Duration.seconds(30),
    handler: "lambda.main",
  };

  const lambdaFunction = new lambda.Function(scope, props.functionName, {
    ...defaultProps,
    ...props,
  });

  return lambdaFunction;
}

function createGoLambda(scope: Construct, props: LambdaFunctionProps): lambda.Function {
  // Golang hanler must be named "bootstrap"
  const handler = 'bootstrap';

  // Create and zip the dummy Go Lambda function
  const tempDir = path.join(os.tmpdir(), 'dummy-go');
  const zipFilePath = createDummyGoPackage(tempDir, handler);

  const defaultProps: Pick<lambda.FunctionProps, 'runtime' | 'code' | 'timeout' | 'handler'> = {
    runtime: lambda.Runtime.PROVIDED_AL2,
    code: lambda.Code.fromAsset(zipFilePath),
    timeout: Duration.seconds(30),
    handler: handler,
  };

  const mergedPropsprops: lambda.FunctionProps = {
    ...defaultProps,
    ...props,
  };

  const lambdaFunction = new lambda.Function(scope, props.functionName, mergedPropsprops);

  fs.rmSync(tempDir, { recursive: true, force: true });

  return lambdaFunction;
}

function createDummyGoPackage(tempDir: string, handlerName: string): string {
  // Create the temporary directory
  if (fs.existsSync(tempDir)) {
    fs.rmdirSync(tempDir, { recursive: true });
  }
  fs.mkdirSync(tempDir);

  // Create a dummy Go file
  const gopath = path.join(tempDir, 'main.go');
  fs.writeFileSync(gopath, "package main; func main() {}");

  // Compile the Go binary and zip it in one step
  const zippath = path.join(tempDir, 'function.zip');
  child_process.execSync(`GOOS=linux GOARCH=amd64 go build -o ${handlerName} ${gopath} && zip -j ${zippath} ${handlerName}`, { cwd: tempDir });

  return zippath;
}
