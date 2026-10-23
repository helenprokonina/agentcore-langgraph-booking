# Stayfinder

A conversational hotel-search app built with Streamlit, LangGraph, Amazon
Bedrock, and Amazon Bedrock AgentCore. Hotel results come from Booking.com
through the HasData MCP service. The app searches only; it does not book stays
or process payments.

## How It Works

The agent collects a destination, future check-in and check-out dates, adult
and room counts, currency, and a total-stay budget. Once the details are valid,
it searches one page of accommodations and ranks matching results by guest
review score, then price. The UI displays up to the returned set of hotels,
while the agent's textual answer lists the top five.

The current search supports adults only. Budget covers the entire stay for all
adults and rooms and must be at least 20 in the requested currency. Bedrock and
AgentCore usage can incur AWS charges, and successful searches use HasData credits.

## Project Structure

```text
streamlit_app.py       Streamlit chat and hotel results
src/agent.py          LangGraph workflow, Bedrock model, and MCP search tool
src/agent_client.py   Local HTTP and AWS runtime clients
requirements.txt      Python dependencies
```

## Prerequisites

- Python 3.11 or newer (`typing.NotRequired` is used).
- AWS credentials configured through the standard AWS credential chain, such
  as an AWS CLI profile or IAM role. Do not put credentials in source code.
- Access to the configured Bedrock model in the chosen region.
- A HasData API key stored in AWS Secrets Manager as `hotel-agent/mcp-api-key`
  in the same region as the agent.

Create the secret through the AWS console using either the key as plain text
or a JSON object with an `api_key` or `HASDATA_API_KEY` field. The agent reads
this secret directly; setting `HASDATA_API_KEY` as an environment variable
alone does not configure authentication.

The identity running the agent needs `secretsmanager:GetSecretValue` for this
secret and permission to invoke the chosen Bedrock model. A secret encrypted
with a customer-managed KMS key may also require `kms:Decrypt`.

## Install

Run these commands from the project root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` instead of
`python` in the commands below.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AWS_DEFAULT_REGION` | `us-east-1` | Region for Bedrock, Secrets Manager, and runtime invocation |
| `BEDROCK_MODEL_ID` | `amazon.nova-pro-v1:0` | Model used by the agent |
| `AGENTCORE_RUNTIME_ARN` | None | Deployed runtime used by the Streamlit AWS connection |

For example, set the region in each terminal before starting its process:

```powershell
$env:AWS_DEFAULT_REGION = "us-east-1"
$env:BEDROCK_MODEL_ID = "amazon.nova-pro-v1:0"
```

The agent loads a local `.env` file if present. The Streamlit client does not
explicitly load that file, so use shell environment variables or its sidebar
for client configuration. `.env` files are excluded from Git.

## Run Locally

In the first terminal, start the agent:

```powershell
python src/agent.py
```

In a second terminal using the same environment, start the UI:

```powershell
python -m streamlit run streamlit_app.py
```

Open the URL printed by Streamlit (normally `http://localhost:8501`). Select
**Local dev** in the sidebar and use `http://127.0.0.1:8080` as the agent URL.
Local mode still calls AWS Bedrock, Secrets Manager, and HasData; it is not offline.

## Use an AWS Runtime

In this mode, the agent runs in AWS AgentCore and the Streamlit UI runs on your
computer. You do not need to run `python src/agent.py` locally. These steps do
not host the Streamlit website on AWS.

The commands below use the Python `bedrock-agentcore-starter-toolkit` installed
by this project's requirements. This toolkit now reports that it is no longer
supported and recommends the newer `@aws/agentcore` CLI. Its commands are not
necessarily interchangeable with the newer CLI. These instructions match the
installed starter toolkit; migration is separate from this setup.

### 1. Prepare AWS Access

Activate the virtual environment from the installation section and work from
the project root. Configure AWS credentials for the account you will deploy to.
If you use AWS CLI v2 with IAM Identity Center, for example:

```powershell
aws configure sso
$env:AWS_PROFILE = "<your-configured-profile>"
aws sso login --profile $env:AWS_PROFILE
$env:AWS_DEFAULT_REGION = "us-east-1"
aws sts get-caller-identity
```

Skip SSO configuration if you already use another valid AWS credential method.
The AWS CLI must be installed separately to run these `aws` commands.

The deploying identity needs permissions to manage AgentCore resources and the
build resources used by the toolkit, including CodeBuild, ECR, and S3. It also
needs permission to pass the selected IAM execution roles, and to create roles
if you ask the toolkit to create them. Follow your organization's IAM policy;
do not grant administrator access just to bypass deployment errors.

Before deploying, create the `hotel-agent/mcp-api-key` secret in your chosen
region as described in Prerequisites. The runtime execution role needs
`secretsmanager:GetSecretValue` for that secret and `bedrock:InvokeModel` for
the configured model, plus any required KMS or inference-profile permissions.
Keep the runtime's CloudWatch logging permissions enabled.

### 2. Configure and Deploy the Agent

For a new deployment, run:

```powershell
agentcore configure --entrypoint src/agent.py --requirements-file requirements.txt --name booking_langgraph_agent --region us-east-1 --deployment-type container --disable-memory
```

Complete the interactive configuration, selecting or creating the appropriate
execution roles. `--disable-memory` disables toolkit-managed AgentCore Memory;
the application uses LangGraph's process-local `InMemorySaver` instead. Keep
HTTP as the runtime protocol. For the current public HasData MCP endpoint, the
runtime needs outbound HTTPS access.

If you already have a valid `.bedrock_agentcore.yaml` for your deployment,
skip configuration and use it. Do not reuse another account's generated
configuration without reviewing its roles, resource names, region, and paths.

Deploy to AWS with the model and region explicitly set for the runtime:

```powershell
agentcore deploy --env AWS_DEFAULT_REGION=us-east-1 --env BEDROCK_MODEL_ID=amazon.nova-pro-v1:0
agentcore status
```

For container deployments, the default deploy command builds an ARM64 image
using AWS CodeBuild, so local Docker is not required. Deployment provisions
AWS resources and can incur charges. Wait until the runtime is `READY`; if the
build fails, inspect its CodeBuild logs before attempting invocation.

Local credentials and shell settings are not automatically inherited by the
runtime. Use its IAM execution role for AWS access, not uploaded access keys.
Keep `.env` and other credentials out of the deployment package as well as Git.

### 3. Connect Streamlit to AWS

Copy the runtime ARN from the deployment output or AgentCore console. It has
the form `arn:aws:bedrock-agentcore:<region>:<account-id>:runtime/<runtime-id>`.
In a terminal with your virtual environment and AWS credentials available:

```powershell
$env:AWS_DEFAULT_REGION = "us-east-1"
$env:AGENTCORE_RUNTIME_ARN = "<your-agentcore-runtime-arn>"
python -m streamlit run streamlit_app.py
```

Select **AWS runtime** and verify the ARN and region in the sidebar. The local
AWS identity running Streamlit needs `bedrock-agentcore:InvokeAgentRuntime`
permission for that runtime, separately from the runtime execution role. Open
the Streamlit URL, choose **New trip**, and send a message. The agent should
ask for any missing travel details before searching. A successful first reply
checks invocation and model access, but a hotel search is needed to verify
Secrets Manager access and HasData authentication.

If present, `.bedrock_agentcore.yaml` supplies default runtime and region
settings. It and the generated `.bedrock_agentcore/` directory are ignored
because they contain local paths and account-specific deployment information.
Each developer should generate their own deployment configuration.

### 4. Update or Troubleshoot the Deployment

After changing agent code or dependencies, rerun the deploy command from step
2, wait for `READY`, and start a new trip. Editing local files does not update
an already deployed agent. Changes only to the Streamlit UI or client require
restarting Streamlit, not redeploying the agent.

For runtime errors, open CloudWatch in the deployment region, search Log groups
for `/aws/bedrock-agentcore/runtimes/` and your runtime ID, and inspect the latest
application log stream around the request time. Invocation failures that occur
before the container executes may not produce an application log. In that case,
inspect the original AWS `ClientError` in the client rather than relying on the
generic UI message. See Troubleshooting below for other common failures.

## Conversation State

The graph uses `InMemorySaver` to retain messages and collected search details
by session ID. This state is process-local, is not durable across restarts,
and is not shared between runtime processes. **New trip** starts a fresh session.

## Troubleshooting

- **Cannot reach the local agent:** confirm the agent process is running and
  the sidebar URL matches its listening address.
- **AWS invocation fails:** confirm the runtime is ready, the region and ARN
  match, and the caller has runtime invocation permission. Check the underlying
  AWS `ClientError` and runtime CloudWatch logs; the UI currently replaces some
  AWS errors with a generic message. Failures before container execution may
  not appear in application logs.
- **CloudWatch logs:** in the runtime's region, open Log groups and search for
  `/aws/bedrock-agentcore/runtimes/`, then the runtime ID. Inspect recent log
  streams around the failed request time.
- **HasData authentication or search fails:** verify the Secrets Manager value,
  runtime role access, HasData permissions, and available credits.
- **Missing structured hotel results:** deploy the current agent code, or
  restart the local agent, then start a new trip.

## Before Publishing

Review `git status --short` and the staged diff before committing. Do not commit
AWS credentials, API keys, local `.env` files, or deployment output. `.gitignore`
does not remove files that are already tracked. If secrets have already been
committed, rotate them and remove them from repository history before publishing.