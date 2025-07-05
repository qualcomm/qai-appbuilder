# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import asyncio
import json
from json_repair import repair_json
import time
import argparse
import os
import sys
import re
import ast
import uuid
import uvicorn
import base64
import numpy as np
import glob
from contextlib import asynccontextmanager
from typing import Dict, List, Literal, Optional, Union, Any, TypedDict
from typing_extensions import TypeAlias
from pydantic import BaseModel, Field, computed_field
from pydantic_settings import BaseSettings
from fastapi import FastAPI, HTTPException, status, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from ChainUtils import GenieLLM

sys.path.append("python")

import utils.install as install
import stable_diffusion_v2_1.stable_diffusion_v2_1 as stable_diffusion

sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')

##########################################################################

HOST="0.0.0.0"
PORT=8910

DEBUG_FASTAPI = False
DEBUG_TOOLCALLS = False
DEBUG_BODY = False

# For QWen, use it's special tool calls prompt format.
USE_QWEN_TOOLCALLS_PROMPT = False

APP_PATH="genie\\python\\"
DEFAULT_MODEL = "IBM-Granite"
TOOLS_MAX_SIZE = 4096 - 2048
TOOLS_NO_RESULT = "[ No input result from tools. ]"

##########################################################################

_client_connected = False
llm = None
_current_model = ""
_model_list = None
_max_query_times = 0
_all_text = False
_enable_thinking = False
_profile = False
_tools_output = ""

class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    END = '\033[0m'

##########################################################################

FunctionParameters: TypeAlias = Dict[str, object]

class FunctionDefinition(TypedDict, total=False):
    name: str
    description: str
    parameters: FunctionParameters
    strict: Optional[bool]

class ChatCompletionToolParam(TypedDict, total=False):
    type: Literal["function"]
    function: FunctionDefinition

class ImageGenerationRequest(BaseModel):
    model: str = DEFAULT_MODEL
    prompt: str = ""
    n: int = 1
    seed: Optional[int] = None
    size: str = "512x512"
    response_format: str = "url"

class ImageData(BaseModel):
    b64_json: str = ""
    revised_prompt: str = ""
    url: str = ""

class ImageGenerationResponse(BaseModel):
    created: int = Field(default_factory=lambda: int(time.time()))
    data: List[ImageData]

class Function(BaseModel):
    name: str = None
    arguments: str = None

class ToolCalls(BaseModel):
    id: Optional[str] = None
    function: Function = None
    type: Optional[str] = None

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[ToolCalls]] = None

class ChatUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class ChatCompletionResponseChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Literal["stop", "length", "tool_calls"] = "stop"

class DeltaMessage(BaseModel):
    role: Optional[Literal["system", "user", "assistant", "tool"]] = None
    content: Optional[str] = None
    tool_calls: Optional[List] = None

class ChatCompletionResponseStreamChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: Optional[Literal["stop", "length", "tool_calls"]] = None

class ChatCompletionResponse(BaseModel):
    id: str = "genie-llm"
    model: str = DEFAULT_MODEL
    object: Literal["chat.completion", "chat.completion.chunk"]
    created: int = Field(default_factory=lambda: int(time.time()))
    choices: Union[List[ChatCompletionResponseChoice], List[ChatCompletionResponseStreamChoice]]
    usage: ChatUsage

class ChatCompletionRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: List[ChatMessage]
    stream: bool = False
    tools: List[ChatCompletionToolParam] = []
    tool_choice: Literal["auto", "none"] = "auto"
    n_ctx: int = Field(default=4096, ge=0)
    temp: float = Field(default=0.8)
    top_k: int = Field(default=1, ge=0)
    top_p: float = Field(default=1.0)

class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    owned_by: str = "owner"
    permission: List = []

class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: List[ModelCard] = []

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "object": "list",
                    "data": [{"id": "QWen-7B", "object": "model", "owned_by": "owner", "permission": []}],
                }
            ]
        }
    }

# https://github.com/tiangolo/fastapi/discussions/11360
class RequestCancelledMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Let's make a shared queue for the request messages
        queue = asyncio.Queue()

        async def message_poller(sentinel, handler_task):
            nonlocal queue
            while True:
                message = await receive()
                # print("Received message:", message)
                if message["type"] == "http.disconnect":
                    handler_task.cancel()
                    return sentinel # Break the loop

                # Puts the message in the queue
                await queue.put(message)

        sentinel = object()
        handler_task = asyncio.create_task(self.app(scope, queue.get, send))
        asyncio.create_task(message_poller(sentinel, handler_task))

        try:
            return await handler_task
        except asyncio.CancelledError:
            global _client_connected
            _client_connected = False
            llm.stop()
            print(f"{Colors.GREEN}INFO:     Cancelling request due to client disconnect", f"{Colors.END}")

##########################################################################

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
        print("INFO:     Process exiting.")
        global llm
        if llm is not None:
            del(llm)
            llm = None
    except Exception as e:
        print("ERROR:    " + str(e))

app = FastAPI(title="Genie REST API", debug=True,
        description="Summary of what my REST API does",
        lifespan=lifespan)

app.add_middleware(RequestCancelledMiddleware)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

from fastapi.staticfiles import StaticFiles
image_path = os.path.join(APP_PATH, "images")
if not os.path.exists(image_path):
    os.makedirs(image_path)
app.mount("/images", StaticFiles(directory = image_path), name = "images")

##########################################################################

FN_FLAG = '✿'
FN_NAME = '✿FUNCTION✿'
FN_ARGS = '✿ARGS✿'
FN_RESULT = '✿RESULT✿'
FN_EXIT = '✿RETURN✿'
FN_FLAG_N = '<'
FN_NAME_N = '<tool_call>'

qwen_stop_sequence_normal = "{\n  \"stop-sequence\" : [\"✿RESULT✿:\", \"✿RETURN✿:\"]\n}"
qwen_stop_sequence_tools = "{\n  \"stop-sequence\" : [\"✿RESULT✿:\", \"✿RETURN✿:\", \"✿FUNCTION✿:\", \"✿ARGS✿:\"]\n}"

def is_qwen_model():
    return "qwen" in _current_model.lower() and USE_QWEN_TOOLCALLS_PROMPT

def format_tools_prompt(tools, lang="en"):
    result = ""
    param_tip = ""
    insert_tip = ""

    # Handle tools call for QWen. Not support the old function call, only support OpenAI tools call.
    # https://github.com/QwenLM/Qwen-Agent/blob/main/qwen_agent/llm/fncall_prompts/qwen_fncall_prompt.py
    if is_qwen_model():  # For QWen
        if lang == "cn":
            result = "\n\n## 工具\n\n你拥有如下工具：\n\n"
            param_tip = "此工具的输入应为JSON对象。"
            insert_tip = (
                "## 你可以在回复中插入以下命令以并行调用N个工具：\n\n"
                "✿FUNCTION✿: 工具1的名称，必须是[{names}]之一\n"
                "✿ARGS✿: 工具1的输入，使用这样的格式: {{\"city\": \"上海\", \"temperature\": \"20度\", ...}}\n"
                "✿FUNCTION✿: 工具2的名称\n"
                "✿ARGS✿: 工具2的输入\n"
                "...\n"
                "✿FUNCTION✿: 工具N的名称\n"
                "✿ARGS✿: 工具N的输入\n"
                "✿RESULT✿: 工具1的结果\n"
                "✿RESULT✿: 工具2的结果\n"
                "...\n"
                "✿RESULT✿: 工具N的结果\n"
                "✿RETURN✿: 根据工具结果进行回复。\n"
            )
        else:
            result = "\n\n## Tools\n\nYou have access to the following tools:\n\n"
            param_tip = "Format the arguments as a JSON object."
            insert_tip = (
                "## Insert the following command in your reply when you need to call N tools in parallel:\n\n"
                "✿FUNCTION✿: The name of tool 1, should be one of [{names}]\n"
                "✿ARGS✿: The input of tool 1, use this formant: {{\"city\": \"Shanghai\", \"temperature\": \"20 degrees\", ...}}\n"
                "✿FUNCTION✿: The name of tool 2\n"
                "✿ARGS✿: The input of tool 2\n"
                "...\n"
                "✿FUNCTION✿: The name of tool N\n"
                "✿ARGS✿: The input of tool N\n"
                "✿RESULT✿: The result of tool 1\n"
                "✿RESULT✿: The result of tool 2\n"
                "...\n"
                "✿RESULT✿: The result of tool N\n"
                "✿RETURN✿: Reply based on tool results.\n"
            )

    # Handle tools call for all kinds of models. Not support the old function call, only support OpenAI tools call.
    # https://github.com/QwenLM/Qwen-Agent/blob/main/qwen_agent/llm/fncall_prompts/nous_fncall_prompt.py
    else: # For other models.
        result = ""
        param_tip = ""
        insert_tip = (
            "\n\n# Tools\n\n"
            "You may call one or more functions to assist with the user query.\n\n"
            "You are provided with function signatures within <tools></tools> XML tags:\n"
            "<tools>\n"
            "{tool_descs}"
            "</tools>\n\n"
            "For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n"
            "<tool_call>\n"
            "{{\"name\": <function-name>, \"arguments\": <args-json-object>}}\n"
            "</tool_call>\n"
        )

    tool_names = []
    length = 0
    tool_descs = ""

    for tool in tools:
        if is_qwen_model():  # For QWen
            tool_str = ""
            func = tool['function']
            name = func['name']
            description = func['description']
            parameters = func['parameters']

            tool_str += f"### {name}\n\n"
            tool_str += f"{name}: {description} Parameters: {json.dumps(parameters)} {param_tip}\n\n"

            tool_length = llm.get_num_tokens(tool_str)
            if length <= TOOLS_MAX_SIZE and tool_length <= TOOLS_MAX_SIZE:
                tool_descs += tool_str
                tool_names.append(name)
                length += tool_length
            else:
                print("Drop tool due to length exceed: ", name, tool_length)
        else: # For other models.
            func = tool['function']
            name = func['name']
            tool_str = json.dumps(tool)
            tool_length = llm.get_num_tokens(tool_str)
            if length <= TOOLS_MAX_SIZE and tool_length <= TOOLS_MAX_SIZE:
                tool_descs += tool_str
                tool_descs += "\n"
                length += tool_length
            else:
                print("Drop tool due to length exceed: ", name, tool_length)

    if length == 0:
        return ""

    if is_qwen_model():  # For QWen
        result += tool_descs + insert_tip.format(names=",".join(tool_names))
    else: # For other models.
        result += insert_tip.format(tool_descs=tool_descs)

    return result

def preprocess_tools_tokens(chunk_text, tools_output):
    global _tools_output

    output = tools_output
    keep_chunk = ""
    has_flag = FN_FLAG in chunk_text or FN_FLAG_N in chunk_text

    if output:
        _tools_output += chunk_text
    elif has_flag:
        result = ""
        pos = chunk_text.find(FN_FLAG)
        if pos == -1:
            pos = chunk_text.find(FN_FLAG_N)

        if pos != -1:
            result = chunk_text[pos:]
            keep_chunk = chunk_text[:pos]

        output = True
        _tools_output += result

    if ((len(_tools_output) > len(FN_NAME)) and (FN_NAME not in _tools_output)) and ((len(_tools_output) > len(FN_NAME_N)) and (FN_NAME_N not in _tools_output)):
        output = False
        _tools_output = ""

    return output, keep_chunk

def convert_tool_calls(content): # Convert tool calls to Qwen format
    matches = re.findall(r'^\s*(\{"name":.*\})', content, re.MULTILINE)
    result = []

    for matche in matches:
        try:
            for line in matche.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                name = obj.get("name")
                args = obj.get("arguments", {})
                result.append(f'✿FUNCTION✿: {name}\n✿ARGS✿: {json.dumps(args, ensure_ascii=False)}')
        except Exception as e:
            print("ERROR:    " + str(e) + " - " + line)
            try:
                line = repair_json(line)
                obj = json.loads(line)
                name = obj.get("name")
                args = obj.get("arguments", {})
                result.append(f'✿FUNCTION✿: {name}\n✿ARGS✿: {json.dumps(args, ensure_ascii=False)}')
            except Exception as e:
                print("ERROR:    " + str(e) + " - " + line)

    return "\n".join(result)

# We need to remove the context if there're additional lines which is not about tools.
def format_tools_output_qwen(tools_output):
    global _tools_output
    tools_output = re.sub(r'\n+', '\n', tools_output).strip() # Remove blank line.
    _tools_output = tools_output

    if not is_qwen_model(): # Handle none Qwen case, convert the data to Qwen format first.
        tools_output = convert_tool_calls(tools_output)

    lines = tools_output.splitlines()
    tools_output = [line for line in lines if line.startswith("✿FUNCTION✿:") or line.startswith("✿ARGS✿:")] # Remove the lines without '✿FUNCTION✿:' and '✿ARGS✿:'
    tools_output = "\n".join(tools_output)
    tools_output = re.sub(r'^.*:\s*$', '', tools_output, flags=re.MULTILINE)  # Remove '✿FUNCTION✿:' and '✿ARGS✿:' which without content.
 
    if is_qwen_model():
        _tools_output = tools_output

    return tools_output

def filter_tool_calls(text):
    pattern = re.compile(r'^\s*(<tool_call>|</tool_call>|{"name":.*)', re.MULTILINE)
    matches = pattern.findall(text)
    return "\n".join(matches)

def format_tools_to_openai(text):
    global _tool_call_id, _tools_output
    
    _tools_output = filter_tool_calls(text)
    text_qwen = format_tools_output_qwen(_tools_output) # preprocess the text.

    _tool_call_id = ""

    lines = [line.strip() for line in text_qwen.strip().splitlines() if line.strip()]
    tools = []
    try:
        i = 0
        while i < len(lines):
            if lines[i].startswith('✿FUNCTION✿:'):
                func_name = lines[i].split(':', 1)[1].strip()
                args_list = []
                i += 1
                while i < len(lines) and lines[i].startswith('✿ARGS✿:'):
                    arg_str = lines[i].split(':', 1)[1].strip()
                    args_list.append(arg_str)
                    i += 1
                if len(args_list) == 0:
                    continue
                parsed_args = []
                for a in args_list:
                    try:
                        parsed_args.append(ast.literal_eval(a))
                    except Exception as e:
                        print("ERROR:    " + str(e))
                        parsed_args.append(a)

                if all(isinstance(arg, dict) for arg in parsed_args):
                    merged = {}
                    for d in parsed_args:
                        merged.update(d)
                    arguments = json.dumps(merged, ensure_ascii=False)
                elif all(isinstance(arg, list) for arg in parsed_args):
                    val = parsed_args[0][0] if parsed_args and parsed_args[0] else ""
                    arguments = json.dumps({"input": val}, ensure_ascii=False)
                else:
                    # arguments = json.dumps(parsed_args[0], ensure_ascii=False) if parsed_args else ""
                    arguments = str(parsed_args[0])

                tool_call_id = f"call_{uuid.uuid4().hex[:8]}"
                _tool_call_id += tool_call_id + ","
                tools.append({
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": func_name,
                        "arguments": arguments
                    }
                })
            else:
                i += 1
    except Exception as e:
        print("ERROR:    " + str(e))

    return tools

##########################################################################
def process_tools_prompt(tools, question):
    global _tools_output

    role = _history[-1].role
    use_tools = False
    tool_no_input = False
    tools_prompt = ""
    tools_query = ""
    tool_content = ""

    if role == "tool": # 2 step: extract tool content for LLM.
        tool_content = [msg.content.strip() for msg in _history if msg.role == "tool" and msg.tool_call_id in _tool_call_id]
        tool_content_str = "\n".join(tool_content).strip()
        if len(tool_content_str) == 0:
            print("[Error]: tool_content is None")
            tool_no_input = True
            return False, "", "", tool_no_input

        # print(llm.get_num_tokens(tool_content))
        #if llm.get_num_tokens(tool_content) > TOOLS_MAX_SIZE:
        #    tool_content = tool_content[:TOOLS_MAX_SIZE]
        #    print("Drop part of the tool content due to exceed max size")
            # print(llm.get_num_tokens(tool_content))

        last_query = llm.get_last_query()

        if len(_tools_output) > 0:
            last_query += _tools_output

            # TODO: Handle none QWen case.
            if is_qwen_model():
                tool_content = "\n".join(tool_content)
                last_query += "\n" + FN_RESULT + ": " + tool_content + "\n" + FN_EXIT + ": "
            else:
                tool_content = "<tool_response>\n" + "\n</tool_response>\n<tool_response>\n".join(tool_content) + "\n</tool_response>\n"
                last_query += "\n" + llm.get_prompt_tool() + tool_content + llm.get_prompt_assistant()

            tools_query = last_query
            if DEBUG_TOOLCALLS:
                print("tools_query: ")
                print(tools_query)
                # print(llm.get_num_tokens(tools_query))
        else:
            print("[Error]: _tools_output is None")

        if is_qwen_model():
            llm.set_stop_sequence(qwen_stop_sequence_tools)

    else: # 1 step: format tool prompt for LLM.
        if len(tools) > 0:
            lang = "cn" if has_chinese(question) else "en"
            tools_prompt = format_tools_prompt(tools, lang)
            if DEBUG_TOOLCALLS:
                print("tools_prompt: ")
                print(tools_prompt)

            if len(tools_prompt) > 0:
                use_tools = True
                _tools_output = ""

        if is_qwen_model():
            llm.set_stop_sequence(qwen_stop_sequence_normal)

    return use_tools, tools_query, tools_prompt, tool_no_input

def has_chinese(text):
    pattern = re.compile(r'[\u4e00-\u9fff]')
    match = pattern.search(text)
    return match is not None

def system_prompt(history):
    if history[0].role == "system":
        prompt = history[0].content
        if prompt is not None and len(prompt) > 0:
            return prompt
    return None

def print_profile():
    if _profile:
        _, _, _, profile = llm.get_profile_str()

        profile = profile.replace("<small><small>", "")
        profile = profile.replace("</small></small>", "")

        print()
        print(f"{Colors.YELLOW}PROF:     ", profile, f"{Colors.END}")

def make_chat_response(content: str = "", tool_calls=None, finish_reason: str = "stop", 
                       model: str = DEFAULT_MODEL, object_type: str = "chat.completion"):
    return ChatCompletionResponse(
        object=object_type,
        model=model,
        choices=[
            ChatCompletionResponseChoice(
                message=ChatMessage(role="assistant", content=content, tool_calls=tool_calls),
                finish_reason=finish_reason
            )
        ],
        usage=ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),  # TODO: add real data here.
    )

def make_stream_response(content: str = None, tool_calls=None, finish_reason: str = None, 
                         role: str = "assistant"):
    delta = DeltaMessage(role=role)
    if content is not None:
        delta.content = content
    if tool_calls is not None:
        delta.tool_calls = tool_calls

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4()}",
        object="chat.completion.chunk",
        created=int(time.time()),
        model=DEFAULT_MODEL,
        choices=[
            ChatCompletionResponseStreamChoice(
                delta=delta,
                finish_reason=finish_reason
            )
        ],
        usage=ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),  # TODO: add real data here.
    )

async def stream_chat_event_publisher(tools):
    global _client_connected, _tools_output, _all_text

    try:
        tools_output = False
        keep_chunk = ""

        question = _history[-1].content
        sys_prompt = system_prompt(_history)
        use_tools, tools_query, tools_prompt, tool_no_input = process_tools_prompt(tools, question)

        if not use_tools:
            # Make sure the first token is blank. https://github.com/langflow-ai/langflow/issues/5338
            chunk = make_stream_response(content="")
            await asyncio.sleep(0.01)
            yield chunk.model_dump_json(exclude_unset=True)

        if tool_no_input: # role == 'tool' but there is no result content from tools, it's abnormal case, return directly.
            chunk = make_stream_response(content=TOOLS_NO_RESULT, finish_reason="stop")
            await asyncio.sleep(0.01)
            yield chunk.model_dump_json(exclude_unset=True)

            await asyncio.sleep(0.01)
            yield "[DONE]"
            _client_connected = False
            return

        for chunk_text in llm.stream(question, sys_prompt=sys_prompt, tools_prompt=tools_prompt, tools_query=tools_query):
            if(len(chunk_text) > 0):
                if _client_connected == False:
                    print("Client disconnected, stop streaming")

                # print(chunk_text, end="", flush=True)

                if use_tools:
                    tools_output, keep_chunk = preprocess_tools_tokens(chunk_text, tools_output)

                if tools_output and keep_chunk == "" and not _all_text: # Not send tools data to client.
                    continue
                #elif keep_chunk != "":
                #    chunk_text = keep_chunk

                chunk = make_stream_response(content=chunk_text)

                await asyncio.sleep(0.01)
                yield chunk.model_dump_json(exclude_unset=False)

        tool_calls = None
        finish_reason = "stop"

        if use_tools and tools_output:
            if DEBUG_TOOLCALLS:
                print("\n_tools_output: ")
                print(_tools_output)

            result = format_tools_to_openai(_tools_output)

            if DEBUG_TOOLCALLS:
                print("_tools_output", _tools_output)
                print("result", result)

            tool_calls = result
            finish_reason = "tool_calls"

            chunk = make_stream_response(tool_calls=tool_calls)

            # print("*" * 20)
            await asyncio.sleep(0.01)
            yield chunk.model_dump_json(exclude_unset=True)

        chunk = make_stream_response(finish_reason=finish_reason)
        await asyncio.sleep(0.01)
        yield chunk.model_dump_json(exclude_unset=True)

        _client_connected = False
        print_profile()

        await asyncio.sleep(0.01)
        yield "[DONE]"

    except asyncio.CancelledError as e:
        raise e

async def stream_chat_event_empty():
    global _client_connected

    try:
        # https://github.com/langflow-ai/langflow/issues/5338
        # Make sure the first token is blank.
        chunk = make_stream_response(content="")
        await asyncio.sleep(0.01)
        _client_connected = False
        yield chunk.model_dump_json(exclude_unset=True)

    except asyncio.CancelledError as e:
        raise e

async def stream_chat(body: ChatCompletionRequest) -> ChatCompletionResponse:
    global _stream_answer, _history_answer, _history, _client_connected, _tools_output

    finish_reason = "stop"

    if DEBUG_BODY:
        print(body)

    model_name = body.model
    loaded = model_load(model_name)

    tools = []
    if body.tools is not None:
        tools = body.tools

    if _client_connected:
        print("INFO:    There is connection in using.")

    if _client_connected or (not loaded and len(_current_model) < 2):  # if loaded failed and no model is loaded.
        if body.stream:
            generator = stream_chat_event_empty()
            return EventSourceResponse(generator, media_type="text/event-stream")
        else:
            return make_chat_response()

    _stream_answer = ""
    _history_answer = ""

    # d.reset()

    if not body.messages:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty messages")

    messages = [ChatMessage(role=msg.role, content=msg.content, tool_call_id=msg.tool_call_id) for msg in body.messages]
    # print(messages)

    # TODO: set parameters.
    # update_parameters(body)
    #_n_predict = body.n_predict

    _client_connected = True
    _history = messages
    answer = ""

    if body.stream:
        generator = stream_chat_event_publisher(tools)
        return EventSourceResponse(generator, media_type="text/event-stream")
    else:
        question = _history[-1].content
        sys_prompt = system_prompt(_history)
        use_tools, tools_query, tools_prompt, tool_no_input = process_tools_prompt(tools, question)

        if tool_no_input: # role == 'tool' but there is no result content from tools.
            _client_connected = False
            return make_chat_response(content=TOOLS_NO_RESULT, finish_reason="stop")

        answer = llm.invoke(question, sys_prompt=sys_prompt, tools_prompt=tools_prompt, tools_query=tools_query)
        print_profile()

    tools_output = FN_NAME in answer or FN_NAME_N in answer
    tool_calls = None
    finish_reason = "stop"

    if use_tools and tools_output:
        _tools_output = answer
        if not _all_text:
            answer = None

        if DEBUG_TOOLCALLS:
            print("\n_tools_output: ")
            print(_tools_output)

        result = format_tools_to_openai(_tools_output)

        #if DEBUG_TOOLCALLS:
        #    print(_tools_output)
        #    print(result)

        tool_calls = result
        finish_reason = "tool_calls"

    print("=" * 40)
    _client_connected = False

    return make_chat_response(content=answer, tool_calls=tool_calls, finish_reason=finish_reason)

# https://cookbook.openai.com/examples/dalle/image_generations_edits_and_variations_with_dall-e
# https://realpython.com/generate-images-with-dalle-openai-api/
def images_generate(request: ImageGenerationRequest):

    print(request)

    try:
        # Set up parameters for Stable Diffusion
        seed = np.random.randint(low=0, high=9999999999, size=None, dtype=np.int64)
        user_step = 20

        stable_diffusion.setup_parameters(request.prompt, "", seed, user_step, 7.5)
        stable_diffusion.model_initialize()

        # Generate the image
        output_dir = "images"
        os.makedirs(output_dir, exist_ok=True)
        image_data = {"path": ""}

        def callback(result):
            if ((None == result) or isinstance(result, str)):   # None == Image generates failed. 'str' == image_path: generated new image path.
                if (None == result):
                    result = "None"
                else:
                    print("Image saved to '" + result + "'")
                    image_data["path"] = result
                    # print(image_data["path"])
            else:
                result = (result + 1) * 100
                result = int(result / user_step)
                result = str(result)
                print("step : " + result)

        stable_diffusion.model_execute(callback, output_dir, show_image=False)
        image_path = image_data["path"]

        # Encode the image as Base64
        image_base64 = None
        with open(image_path, "rb") as image_file:
            image_base64 = base64.b64encode(image_file.read()).decode("utf-8")

        # Return the response
        return ImageGenerationResponse(
            data = [ImageData(url=f"/images/{os.path.basename(image_path)}", b64_json=image_base64)]
        )
    except Exception as e:
        print("ERROR:    " + str(e))
        raise HTTPException(status_code=500, detail=str(e))

##########################################################################


def download_tokenizer(model_path, url):
    if not os.path.exists(model_path):
        install.download_url(url, model_path)

        if "Phi-3.5-mini" in model_path:
            import re
            with open(model_path, 'r', encoding='utf-8') as f:
                content = f.read()
            pattern = r',\s*{\s*"type":\s*"Strip",\s*"content":\s*"\s*",\s*"start":\s*\d+,\s*"stop":\s*\d+\s*}'
            new_content = re.sub(pattern, '', content)
            with open(model_path, 'w', encoding='utf-8') as f:
                f.write(new_content)

def download():
    download_tokenizer(
        APP_PATH + "\\models\\IBM-Granite-v3.1-8B\\tokenizer.json",
        "https://gitee.com/hf-models/granite-3.1-8b-base/raw/main/tokenizer.json"
    )
    download_tokenizer(
        APP_PATH + "\\models\\Phi-3.5-mini\\tokenizer.json",
        "https://gitee.com/hf-models/Phi-3.5-mini-instruct/raw/main/tokenizer.json"
    )

def model_load(model_name):
    global llm, _current_model, _model_list, _max_query_times, _enable_thinking
    find_model = False

    if model_name.lower() in _current_model.lower():
        return False

    download()
    
    if _model_list is None:
        model_root = APP_PATH + "models"

        _model_list = []
        for f in os.listdir(model_root):
            dir_path = os.path.join(model_root, f)
            if os.path.isdir(dir_path):
                bin_files = glob.glob(os.path.join(dir_path, "*.bin"))
                has_tokenizer = os.path.isfile(os.path.join(dir_path, "tokenizer.json"))
                has_prompt = os.path.isfile(os.path.join(dir_path, "prompt.conf"))
                if bin_files and has_tokenizer and has_prompt:
                    _model_list.append(f)

    for model in _model_list:
        if model_name.lower() in model.lower():
            _current_model = model
            find_model = True
            break

    # print()

    if not find_model:
        print(f"{Colors.RED}INFO:     model name <<<", model_name, f">>> is incorrect! {Colors.END}")
        return False

    if llm is not None:
        del(llm)
        llm = None

    time_start = time.time()

    print(f"{Colors.GREEN}INFO:     loading model <<<", _current_model, f">>>{Colors.END}")
    llm = GenieLLM()

    if _max_query_times == 0:
        _max_query_times = llm.max_query_times
    llm.max_query_times = _max_query_times
    llm.enable_thinking = _enable_thinking

    llm.init(model_name=_current_model)
    print(f"{Colors.GREEN}INFO:     model <<<", _current_model, f">>> is ready!{Colors.END}")
 
    time_end = time.time()
    load_time = str(round(time_end - time_start, 2)) + " (s)"
    print(f"{Colors.GREEN}INFO:     model init time:", load_time, f"{Colors.END}")
    return True

##########################################################################

def shutdown():
    print("INFO:     Process exiting.")
    global llm
    del(llm)
    llm = None
    return Response(status_code=200, content='Server shutting down...')

app.add_api_route('/shutdown', shutdown, methods=['GET'])

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_details = exc.errors()
    print(f"Validation error: {json.dumps(error_details, indent=2)}")
    # print(f"Request body: {await request.body()}")
    return JSONResponse(
        status_code = 422,
        content={"detail": exc.errors()},
    )


@app.middleware("http")
async def log_request_body(request: Request, call_next):
    print(f"Request: {request.method} {request.url}")

    if DEBUG_FASTAPI:
        body = await request.body()
        try:
            body_json = json.loads(body)
            print("Request Body Json: ")
            formatted_json = json.dumps(body_json, indent=2, ensure_ascii=False)
            print(formatted_json)
        except json.JSONDecodeError:
            print(f"Request Body Text: {body.decode('utf-8')}")

    response = await call_next(request)

    print(f"Response status: {response.status_code}")

    return response


def register_post_routes(app: FastAPI):
    # Image generation endpoints
    app.post("/v1/images/generations", response_model=ImageGenerationResponse)(images_generate)
    app.post("/images/generations", response_model=ImageGenerationResponse)(images_generate)
    
    # Chat/completion endpoints
    for path in ["/v1/completions", "/v1/chat/completions", "/completions", "/chat/completions"]:
        app.post(path)(stream_chat)

def get_default_model_cards():
    global _model_list

    if not _model_list:
        return [
            ModelCard(id="QWen2-7B"),
            ModelCard(id="IBM-Granite"),
        ]

    return [ModelCard(id=name) for name in _model_list]

def list_models() -> ModelList:
    return ModelList(data=get_default_model_cards())

def register_get_model_routes(app: FastAPI):
    app.get("/v1/models")(list_models)
    app.get("/models")(list_models)

register_post_routes(app)
register_get_model_routes(app)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--loadmodel", action="store_true")
    parser.add_argument("--modelname", default=DEFAULT_MODEL, type=str)
    parser.add_argument("--all-text", action="store_true")
    parser.add_argument("--enable-thinking", action="store_true")
    parser.add_argument("--max-query-times", default=0, type=int)
    parser.add_argument("--profile", action="store_true")

    args = parser.parse_args()

    _max_query_times = args.max_query_times
    _profile = args.profile
    _all_text = args.all_text
    _enable_thinking = args.enable_thinking

    if args.loadmodel:
        DEFAULT_MODEL = args.modelname
        print(f"{Colors.GREEN}INFO: set default model: <<<", DEFAULT_MODEL, f">>>{Colors.END}")
        model_load(args.modelname)

    uvicorn.run(app, host=HOST, port=PORT, workers=1)
