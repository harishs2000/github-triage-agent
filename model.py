import base64
import os
import time
from dataclasses import dataclass, field

import httpx
from google import genai
from google.genai import errors, types

from tracing import tracer

MODEL_NAME = "gemini-3.5-flash-lite"
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 5
REQUEST_TIMEOUT_MS = 60_000

TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="list_issues",
        description="List issues in the repository, optionally filtered by state.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "state": types.Schema(
                    type="STRING",
                    description="Which issues to list.",
                    enum=["open", "closed", "all"],
                ),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="get_issue",
        description="Get the full details of a single issue by number.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "issue_number": types.Schema(type="INTEGER", description="The issue number."),
            },
            required=["issue_number"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_issues",
        description="Search issues in the repository using GitHub search syntax (e.g. keywords, 'in:title', 'label:bug').",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "query": types.Schema(type="STRING", description="GitHub search query."),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="add_labels",
        description="Add one or more labels to an issue.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "issue_number": types.Schema(type="INTEGER"),
                "labels": types.Schema(type="ARRAY", items=types.Schema(type="STRING")),
            },
            required=["issue_number", "labels"],
        ),
    ),
    types.FunctionDeclaration(
        name="post_comment",
        description="Post a comment on an issue.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "issue_number": types.Schema(type="INTEGER"),
                "text": types.Schema(type="STRING", description="Comment body."),
            },
            required=["issue_number", "text"],
        ),
    ),
    types.FunctionDeclaration(
        name="assign_issue",
        description="Assign a GitHub user to an issue.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "issue_number": types.Schema(type="INTEGER"),
                "username": types.Schema(type="STRING", description="GitHub username to assign."),
            },
            required=["issue_number", "username"],
        ),
    ),
    types.FunctionDeclaration(
        name="close_issue",
        description="Close an issue.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "issue_number": types.Schema(type="INTEGER"),
            },
            required=["issue_number"],
        ),
    ),
]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict = field(default_factory=dict)
    thought_signature: str | None = None


@dataclass
class ModelResponse:
    text: str | None
    tool_calls: list[ToolCall]


def _client():
    # Without an explicit timeout a stalled connection blocks forever: the
    # retry loop below only fires on errors, and a call that never returns
    # never raises one. Observed model calls run 0.6-2.5s, so 60s is far
    # above normal while still failing fast on a hang.
    return genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )


def _to_gemini_contents(messages):
    contents = []
    for msg in messages:
        role = msg["role"]
        if role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=msg["content"])]))
        elif role == "assistant":
            parts = []
            if msg.get("content"):
                parts.append(types.Part(text=msg["content"]))
            for call in msg.get("tool_calls", []):
                signature = call.get("thought_signature")
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(name=call["name"], args=call["args"]),
                        thought_signature=base64.b64decode(signature) if signature else None,
                    )
                )
            contents.append(types.Content(role="model", parts=parts))
        elif role == "tool":
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=msg["name"],
                                response={"result": msg["content"]},
                            )
                        )
                    ],
                )
            )
    return contents


def _generate_with_retry(client, contents, config):
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(model=MODEL_NAME, contents=contents, config=config)
            return response, attempt - 1
        except errors.ClientError as exc:
            if exc.code != 429 or attempt == MAX_ATTEMPTS:
                raise
            last_error = exc
        except errors.ServerError as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            last_error = exc
        except httpx.TimeoutException as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            last_error = exc
        time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    raise last_error


def call_model(system_prompt, messages, tool_declarations=TOOL_DECLARATIONS):
    client = _client()
    contents = _to_gemini_contents(messages)
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[types.Tool(function_declarations=tool_declarations)],
    )

    with tracer.start_as_current_span("call_model") as span:
        start = time.perf_counter()
        response, retry_count = _generate_with_retry(client, contents, config)
        latency_ms = (time.perf_counter() - start) * 1000

        usage = response.usage_metadata
        token_count_in = usage.prompt_token_count if usage else None
        token_count_out = usage.candidates_token_count if usage else None

        span.set_attribute("model.name", MODEL_NAME)
        span.set_attribute("latency_ms", latency_ms)
        span.set_attribute("retry_count", retry_count)
        if token_count_in is not None:
            span.set_attribute("token_count_in", token_count_in)
        if token_count_out is not None:
            span.set_attribute("token_count_out", token_count_out)

    candidate = response.candidates[0]
    text_parts = []
    tool_calls = []
    for i, part in enumerate(candidate.content.parts):
        if part.function_call:
            signature = base64.b64encode(part.thought_signature).decode() if part.thought_signature else None
            tool_calls.append(
                ToolCall(
                    id=f"call_{i}",
                    name=part.function_call.name,
                    args=dict(part.function_call.args),
                    thought_signature=signature,
                )
            )
        elif part.text:
            text_parts.append(part.text)

    return ModelResponse(text="".join(text_parts) or None, tool_calls=tool_calls)
