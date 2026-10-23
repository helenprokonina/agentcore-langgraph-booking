import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import boto3
import httpx
import yaml
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


class AgentInvocationError(RuntimeError):
    pass


def deployment_settings() -> dict:
    settings = {}
    path = Path(__file__).resolve().parents[1] / ".bedrock_agentcore.yaml"
    if path.exists():
        try:
            configuration = yaml.safe_load(path.read_text()) or {}
            agents = configuration.get("agents", {})
            agent = agents.get(configuration.get("default_agent"), {})
            settings = {
                "runtime_arn": agent.get("bedrock_agentcore", {}).get("agent_arn", ""),
                "region": agent.get("aws", {}).get("region", "us-east-1"),
            }
        except (OSError, ValueError, AttributeError, yaml.YAMLError):
            settings = {}
    return {
        "runtime_arn": os.environ.get("AGENTCORE_RUNTIME_ARN", settings.get("runtime_arn", "")),
        "region": os.environ.get("AWS_DEFAULT_REGION", settings.get("region", "us-east-1")),
    }


def decode_response(payload: bytes | str | dict) -> dict:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    for _ in range(5):
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError) as error:
                raise AgentInvocationError("The agent returned an unreadable response.") from error
        if isinstance(payload, dict) and isinstance(payload.get("answer"), str):
            if payload.get("needs_input") is False and not payload.get("search_error"):
                if not isinstance(payload.get("hotels"), list):
                    raise AgentInvocationError(
                        "The connected agent did not return a structured hotels list. "
                        "For AWS runtime, deploy the latest src/agent.py; "
                        "for Local dev, restart the agent server. Then start a new trip."
                    )
            return payload
        if isinstance(payload, dict):
            payload = payload.get("response", payload.get("body"))
        else:
            break
    raise AgentInvocationError("The agent response did not contain an answer.")


def invoke_agent(
    prompt: str,
    session_id: str,
    *,
    connection: str,
    runtime_arn: str = "",
    region: str = "us-east-1",
    local_url: str = "http://127.0.0.1:8080",
) -> dict:
    payload = {"prompt": prompt, "thread_id": session_id}
    if connection == "Local dev":
        address = urlsplit(local_url)
        if address.scheme not in ("http", "https") or not address.netloc:
            raise AgentInvocationError("Enter a valid local agent URL.")
        endpoint = local_url.rstrip("/")
        if not endpoint.endswith("/invocations"):
            endpoint += "/invocations"
        try:
            response = httpx.post(endpoint, json=payload, timeout=240)
            response.raise_for_status()
            return decode_response(response.content)
        except httpx.HTTPStatusError as error:
            raise AgentInvocationError(f"The local agent returned HTTP {error.response.status_code}.") from error
        except httpx.RequestError as error:
            raise AgentInvocationError("Cannot reach the local agent. Check that your dev server is running.") from error
    if connection != "AWS runtime":
        raise AgentInvocationError("Select a supported agent connection.")
    if not runtime_arn.startswith("arn:aws:bedrock-agentcore:"):
        raise AgentInvocationError("Enter a valid AgentCore runtime ARN.")
    try:
        client = boto3.Session(region_name=region).client(
            "bedrock-agentcore",
            config=Config(connect_timeout=10, read_timeout=240, retries={"max_attempts": 0}),
        )
        response = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload).encode("utf-8"),
        )
        body = response["response"]
        try:
            return decode_response(body.read())
        finally:
            body.close()
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in ("AccessDeniedException", "UnauthorizedException"):
            message = "AWS denied access to this agent. Check your local AWS identity and InvokeAgentRuntime permission."
        elif code == "ResourceNotFoundException":
            message = "The agent runtime was not found in this AWS region."
        elif code in ("ThrottlingException", "TooManyRequestsException"):
            message = "The agent is busy. Please try again shortly."
        else:
            message = "AWS could not invoke the agent. Check the runtime status and server logs."
        raise AgentInvocationError(message) from error
    except BotoCoreError as error:
        raise AgentInvocationError("AWS connection failed. Check your local credentials, region and network connection.") from error


def safe_web_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        address = urlsplit(value)
    except ValueError:
        return None
    if address.scheme == "https" and address.netloc and not address.username:
        return value
    return None