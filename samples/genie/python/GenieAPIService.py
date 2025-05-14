import asyncio
import json
import time
import argparse
import os
import sys
import uuid
import uvicorn
import base64
import numpy as np
from contextlib import asynccontextmanager
from typing import Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, computed_field
from pydantic_settings import BaseSettings
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from ChainUtils import GenieLLM

sys.path.append("python")

import stable_diffusion_v2_1.stable_diffusion_v2_1 as stable_diffusion

sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')

##########################################################################

HOST="0.0.0.0"
PORT=8910
APP_PATH="genie\\python\\"

##########################################################################

_client_connected = False
llm = None
_current_model = ""
_model_list = None
_max_query_times = 0

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

class ImageGenerationRequest(BaseModel):
    model: str = "default-model"
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

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class ChatUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class ChatCompletionResponseChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Literal["stop", "length"] = "stop"

class DeltaMessage(BaseModel):
    role: Optional[Literal["system", "user", "assistant"]] = None
    content: Optional[str] = None

class ChatCompletionResponseStreamChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: Optional[Literal["stop", "length"]] = None

class ChatCompletionResponse(BaseModel):
    id: str = "genie-llm"
    model: str = "default-model"
    object: Literal["chat.completion", "chat.completion.chunk"]
    created: int = Field(default_factory=lambda: int(time.time()))
    choices: Union[List[ChatCompletionResponseChoice], List[ChatCompletionResponseStreamChoice]]
    usage: ChatUsage

class ChatCompletionRequest(BaseModel):
    model: str = "default-model"
    messages: List[ChatMessage]
    stream: bool = False
    n_ctx: int = Field(default=2048, ge=0)
    temp: float = Field(default=2023)
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
            print("INFO:    Cancelling request due to disconnect")

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
        print(e)


app = FastAPI(title="Genie REST API",
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

def system_prompt(history):
    if history[0].role == "system":
        prompt = history[0].content
        if prompt is not None and len(prompt) > 0:
            return prompt
    return None

async def stream_chat_event_publisher(history, body):
    global _client_connected

    try:
        question = _history[-1].content
        sys_prompt = system_prompt(_history)
        
        # https://github.com/langflow-ai/langflow/issues/5338
        # Make sure the first token is blank.
        chunk = ChatCompletionResponse(
            id = f"chatcmpl-{uuid.uuid4()}",
            object = "chat.completion.chunk",
            created = int(time.time()),
            choices = [ChatCompletionResponseStreamChoice(delta=DeltaMessage(role="assistant", content=""))],
            usage = ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )
        await asyncio.sleep(0.01)  # yield control back to event loop for cancellation check
        yield chunk.model_dump_json(exclude_unset=True) + "\n\n"

        #print(question)
        for text in llm.stream(question, sys_prompt=sys_prompt):
            if(len(text) > 0):
                # print(text, end="", flush=True)
                chunk = ChatCompletionResponse(
                    id = f"chatcmpl-{uuid.uuid4()}",
                    object = "chat.completion.chunk",
                    created = int(time.time()),
                    choices = [ChatCompletionResponseStreamChoice(delta=DeltaMessage(role="assistant", content=text))],
                    usage = ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),    # TODO: add real data here.
                )

                # print(chunk.model_dump_json(exclude_unset=False) + "\n\n")

                await asyncio.sleep(0.01)  # yield control back to event loop for cancellation check
                yield chunk.model_dump_json(exclude_unset=False) + "\n\n"

        chunk = ChatCompletionResponse(
            id = f"chatcmpl-{uuid.uuid4()}",
            object = "chat.completion.chunk",
            created = int(time.time()),
            choices = [ChatCompletionResponseStreamChoice(delta=DeltaMessage(role="assistant"), finish_reason="stop")],
            usage = ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),    # TODO: add real data here.
        )
        # print("*" * 20)
        await asyncio.sleep(0.01)  # yield control back to event loop for cancellation check
        _client_connected = False
        yield chunk.model_dump_json(exclude_unset=True) + "\n\n"

    except asyncio.CancelledError as e:
        raise e

async def stream_chat_event_empty():
    global _client_connected

    try:
        # https://github.com/langflow-ai/langflow/issues/5338
        # Make sure the first token is blank.
        chunk = ChatCompletionResponse(
            id = f"chatcmpl-{uuid.uuid4()}",
            object = "chat.completion.chunk",
            created = int(time.time()),
            choices = [ChatCompletionResponseStreamChoice(delta=DeltaMessage(role="assistant", content=""))],
            usage = ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )
        await asyncio.sleep(0.01)  # yield control back to event loop for cancellation check
        _client_connected = False
        yield chunk.model_dump_json(exclude_unset=True) + "\n\n"

    except asyncio.CancelledError as e:
        raise e

async def stream_chat(body: ChatCompletionRequest) -> ChatCompletionResponse:
    global _stream_answer, _history_answer, _history, _client_connected

    finish_reason = "stop"

    # print(body)
    model_name = body.model
    loaded = model_load(model_name)

    if _client_connected:
        print("INFO:    There is connection in using.")

    if _client_connected or (not loaded and len(_current_model) < 2):  # if loaded failed and no model is loaded.
        if body.stream:
            generator = stream_chat_event_empty()
            return EventSourceResponse(generator, media_type="text/event-stream")
        else:
            return ChatCompletionResponse(
                object="chat.completion",
                choices=[ChatCompletionResponseChoice(message = ChatMessage(role="assistant", content=""), finish_reason=finish_reason,)],
                usage = ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),    # TODO: add real data here.
            )

    _stream_answer = ""
    _history_answer = ""

    # d.reset()

    if not body.messages:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty messages")

    messages = [ChatMessage(role=msg.role, content=msg.content) for msg in body.messages]
    # print(messages)

    # TODO: set parameters.
    # update_parameters(body)
    #_n_predict = body.n_predict

    _client_connected = True
    _history = messages
    answer = ""

    if body.stream:
        generator = stream_chat_event_publisher(messages, body)
        return EventSourceResponse(generator, media_type="text/event-stream")
    else:
        question = _history[-1].content
        sys_prompt = system_prompt(_history)

        # print(_history)
        # print()
        # print(question)
        answer = llm.invoke(question, sys_prompt=sys_prompt)
        # print(answer)

    print("=" * 40)
    _client_connected = False
    return ChatCompletionResponse(
        object="chat.completion",
        choices=[ChatCompletionResponseChoice(message=ChatMessage(role="assistant", content=answer), finish_reason=finish_reason, )],
        usage = ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),    # TODO: add real data here.
    )

# https://cookbook.openai.com/examples/dalle/image_generations_edits_and_variations_with_dall-e
# https://realpython.com/generate-images-with-dalle-openai-api/
def generate_image(request: ImageGenerationRequest):

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


@app.post("/v1/images/generations", response_model=ImageGenerationResponse)
def images_generate(request: ImageGenerationRequest):
    return generate_image(request)

@app.post("/images/generations", response_model=ImageGenerationResponse)
def images_generate(request: ImageGenerationRequest):  # 定义一个名为images_generate的函数，接受一个GenerationRequest参数request
    return generate_image(request)

@app.post("/v1/completions")
async def completion(body: ChatCompletionRequest) -> ChatCompletionResponse:
    return await stream_chat(body)

@app.post("/v1/chat/completions")
async def chat_completion(body: ChatCompletionRequest) -> ChatCompletionResponse:
    return await stream_chat(body)

@app.post("/completions")
async def completion(body: ChatCompletionRequest) -> ChatCompletionResponse:
    return await stream_chat(body)

@app.post("/chat/completions")
async def chat_completion(body: ChatCompletionRequest) -> ChatCompletionResponse:
    return await stream_chat(body)

@app.get("/v1/models")
async def list_models() -> ModelList:
    return ModelList(data=[ModelCard(id="QWen2-7B"), ModelCard(id="IBM-Granite")])

@app.get("/models")
async def list_models() -> ModelList:
    return ModelList(data=[ModelCard(id="QWen2-7B"), ModelCard(id="IBM-Granite")])

def shutdown():
    print("INFO:     Process exiting.")
    global llm
    del(llm)
    llm = None
    return fastapi.Response(status_code=200, content='Server shutting down...')

app.add_api_route('/shutdown', shutdown, methods=['GET'])

def model_load(model_name):
    global llm, _current_model, _model_list, _max_query_times
    find_model = False

    if model_name.lower() in _current_model.lower():
        return False

    if _model_list is None:
        model_root = APP_PATH + "models"
        _model_list = [f for f in os.listdir(model_root) if os.path.isdir(os.path.join(model_root, f))]

    for model in _model_list:
        if model_name.lower() in model.lower():
            _current_model = model
            find_model = True
            break

    print()
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

    llm.init(model_name=_current_model)
    print(f"{Colors.GREEN}INFO:     model <<<", _current_model, f">>> is ready!{Colors.END}")
 
    time_end = time.time()
    load_time = str(round(time_end - time_start, 2)) + " (s)"
    print(f"{Colors.GREEN}INFO:     model init time:", load_time, f"{Colors.END}")
    return True


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--loadmodel", action="store_true")
    parser.add_argument("--modelname", default="IBM-Granite", type=str)
    parser.add_argument("--max-query-times", default=0, type=int)

    args = parser.parse_args()

    _max_query_times = args.max_query_times

    if args.loadmodel:
        model_load(args.modelname)

    uvicorn.run(app, host=HOST, port=PORT, workers=1)
