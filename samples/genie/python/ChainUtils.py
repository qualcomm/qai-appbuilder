import os
import time
import re
import threading
import json
from typing import Any, Dict, Iterator, List, Mapping, Optional

from langchain_core.language_models.llms import LLM
from langchain_core.callbacks.manager import CallbackManagerForLLMRun
from langchain_core.outputs import Generation, GenerationChunk, LLMResult
from langchain.text_splitter import RecursiveCharacterTextSplitter

from qai_appbuilder import (GenieContext)


###########################################################################

DEBUG_PROMPT = True
DEBUG_OUTPUT = True
DEBUG_GENIE = False

DOCS_MAX_SIZE = 4096 - 1024
DOCS_MAX_QUERY_TIMES = 3

APP_PATH="genie\\python\\"

lib_path = "qai_libs"
if not lib_path in os.getenv('PATH'):
    lib_path = os.getenv('PATH') + ";" + lib_path + ";"
    os.environ['PATH'] = lib_path
    # print(os.getenv('PATH'))

###########################################################################

class GenieLLMCallbackHandler:
    def __init__(self):
        self.chain_step_count = 0

    def on_query_start(self, serialized, prompts, **kwargs):
        raise NotImplementedError("This method should be overridden by subclasses")

    def on_query_end(self, response, **kwargs):
        raise NotImplementedError("This method should be overridden by subclasses")

class GenieModel():
    def __init__(self, 
        model_name: str = "None",
        max_query_times: int = DOCS_MAX_QUERY_TIMES) -> None:

        self.stream_chunk = ""
        self.prompt = ""

        self.exiting = False
        self.generating = False
        # self.prompt_docs = ""
        # self.model_ready = False

        model_path = APP_PATH + "models\\" + model_name
        config_path = model_path + "\\config.json"
        prompt_path = model_path + "\\prompt.conf"
        self.model_name = model_name
        self.max_query_times = max_query_times

        if not os.path.exists(config_path) or not os.path.exists(prompt_path) :
            print("[Error] model config or prompt file not found.")
            return

        with open(prompt_path, 'r') as file:
            prompt_content = file.read()
        prompt_lines = prompt_content.split('\n')
        self.prompt_tags_1 = prompt_lines[0].split(': ')[1].strip()
        self.prompt_tags_2 = prompt_lines[1].split(': ')[1].strip()
        # print(prompt_path)
        # print(prompt_lines)

        # Threading.
        self.thread_cond = threading.Condition()
        self.stream_thread = threading.Thread(target=self.llm_thread)
        self.stream_thread.start()
        self.stream_lock = threading.Lock()

        self.d = GenieContext(config_path, DEBUG_GENIE)
        if not self.d:
            print("[Error] model load failed.")
            return

        if "granite" in self.model_name.lower(): # if 'IBM-Granite' model, we need to set stop sequence.
            stop_sequence = "{\n  \"stop-sequence\" : [\"<|end_of_text|>\", \"<|end_of_role|>\", \"<|start_of_role|>\"]\n}"
            # print(stop_sequence)
            self.d.SetStopSequence(stop_sequence)

    def set_params(self, max_length, temp, top_k, top_p):
        self.d.SetParams(max_length, temp, top_k, top_p)

    def get_profile(self):
        try:
            profile_data = json.loads(self.d.GetProfile())
            profile_events = profile_data['components'][0]['events']
            events_len = len(profile_events)
            profile_event = profile_events[events_len - 1]
            prompt_speed = profile_event['prompt-processing-rate']['value']
            eval_speed = profile_event['token-generation-rate']['value']

            num_prompt_tokens = profile_event['num-prompt-tokens']['value']
            num_generated_tokens = profile_event['num-generated-tokens']['value']
            time_to_first_token = profile_event['time-to-first-token']['value']
            time_to_first_token = time_to_first_token / 1000 # ms
            token_generation_time = profile_event['token-generation-time']['value']
            token_generation_time = token_generation_time / 1000 # ms

            # print(profile_event)

            return prompt_speed, eval_speed, num_prompt_tokens, num_generated_tokens, time_to_first_token, token_generation_time
        except Exception as e:
            print(e)
            return 0, 0, 0, 0, 0, 0

    def llm_thread(self):
        while(True):
            self.thread_cond.acquire()
            self.thread_cond.wait()

            self.stream_chunk = ""

            if self.exiting:
                return

            def response(text):
                self.stream_lock.acquire()
                self.stream_chunk += text
                self.stream_lock.release()

                time.sleep(0.01)  # Let Gradio to upadte Chatbot content.
                if DEBUG_OUTPUT:
                    print(text, end="", flush=True)
                return

            q = re.sub(r'\n\s*\n+', '\n\n', self.prompt)  # Remove extra blank line.

            prompt_tags_1 = self.prompt_tags_1
            if self.sys_prompt is not None:
                prompt_tags_1 = prompt_tags_1.replace("\\nYou are a helpful assistant.", "\\n" + self.sys_prompt)
            prompt_tags_2 = self.prompt_tags_2
            tokens = self.get_num_tokens(q)

            split_question = ""
            split_count = 0
            if tokens > DOCS_MAX_SIZE:
                print(tokens)
                text_splitter = RecursiveCharacterTextSplitter(
                                chunk_size=DOCS_MAX_SIZE, chunk_overlap=0,
                                separators=["\n\n", "\n", "。", "！", "？", ".", "?", "!", " ", ""],
                                length_function=self.get_num_tokens
                            )
                split_question = text_splitter.split_text(q)
                split_count = len(split_question)
                if split_count > self.max_query_times:
                    split_count = self.max_query_times
            else:
                split_question = [q]
                split_count = 1

            # print(split_count)
            for i in range(split_count):
                q = split_question[i]

                q = prompt_tags_1 + q + prompt_tags_2
                if DEBUG_PROMPT:
                    print("=" * 30)
                    print(q)
                #print(len(q))

                # d.reset()  TODO.
                self.d.Query(q, response)

            self.generating = False

    def query(self, prompt, **kwargs: Any,):
        self.prompt = prompt
        self.stream_chunk = ""

        sys_prompt = kwargs.get('sys_prompt', None)
        self.sys_prompt = sys_prompt

        self.thread_cond.acquire()
        self.thread_cond.notify()
        self.thread_cond.release()

        self.generating = True

        while(True):
            self.stream_lock.acquire()
            chunk = self.stream_chunk
            self.stream_chunk = ""
            self.stream_lock.release()

            if(len(chunk) > 0):
                # print(chunk, end="", flush=True)
                yield chunk

            if(False == self.generating):
                break
            time.sleep(0.01)

        if(len(self.stream_chunk) > 0):
            time.sleep(0.01)
            yield self.stream_chunk

    def stop(self):
        self.d.Stop()

    def get_num_tokens(self, text: str) -> int:
        return self.d.TokenLength(text)

    def exit(self):
        if self.generating:
            self.stop()
            time.sleep(1)

        self.exiting = True
        self.thread_cond.acquire()
        self.thread_cond.notify()
        self.thread_cond.release()
        self.stream_thread.join()

        del(self.d)
        self.d = None

    def __del__(self):
        if self.d:
            print("[ERROR] Please call 'exit()' function before delete GenieModel object.")

###########################################################################

class GenieLLM(LLM):
    model_name: str = None
    ready: bool = False
    model: GenieModel = None
    callback_handler: GenieLLMCallbackHandler = None
    max_query_times:int = DOCS_MAX_QUERY_TIMES

    def init(self, model_name, callback_handler: GenieLLMCallbackHandler = None) -> None:
        self.callback_handler = callback_handler
        return self._init(model_name)

    def set_callback(self, callback_handler):
        self.callback_handler = callback_handler

    def get_num_tokens(self, text: str) -> int:
        return self.model.get_num_tokens(text)

    def set_params(self, max_length, temp, top_k, top_p):
        self.model.set_params(max_length, temp, top_k, top_p)

    def stop(self):
        self.model.stop()

    def is_ready(self):
        return self.ready

    def get_profile(self):
        return self.model.get_profile()

    def get_profile_str(self):
        prompt_speed, eval_speed, num_prompt_tokens, num_generated_tokens, time_to_first_token, token_generation_time = self.get_profile()

        prompt_speed = "%.2f" % (prompt_speed)
        eval_speed = "%.2f" % (eval_speed)
        prompt_speed = prompt_speed + " toks/sec (" + str(num_prompt_tokens) + " toks)"
        eval_speed = eval_speed + " toks/sec (" + str(num_generated_tokens) + " toks)"
        time_to_first_token = str(round(time_to_first_token, 2)) + " ms"
        token_generation_time = "generation time:" + str(round(token_generation_time, 2)) + " ms (" + str(round(token_generation_time / 1000, 2)) + " sec)"

        profile = "<small><small>" + prompt_speed + " • " + eval_speed + " • " + time_to_first_token + " to first token" + " • " + token_generation_time + " • [ " + self.model_name + " ]</small></small>"
        return time_to_first_token, prompt_speed, eval_speed, profile

    def _init(self, model_name) -> None:
        self.ready = False
        self.model_name = model_name
        #print(self.model_name)
        self.model = GenieModel(model_name, self.max_query_times)
        self.ready = True

    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        if stop is not None:
            raise ValueError("stop kwargs are not permitted.")

        if self.callback_handler:
            self.callback_handler.on_query_start(None, prompt)

        #print(prompt)
        response = self.model.query(prompt, **kwargs)

        generated_text = ""
        for text in response:
            #print(chunk, end="", flush=True)
            generated_text += text

        if self.callback_handler:
            self.callback_handler.on_query_end(generated_text)

        return generated_text

    def _stream(    # '.stream' will call this function.
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[GenerationChunk]:
        if stop is not None:
            raise ValueError("stop kwargs are not permitted.")

        if self.callback_handler:
            self.callback_handler.on_query_start(None, prompt)

        # print(prompt)
        response = self.model.query(prompt, **kwargs)

        generated_text = ""
        for text in response:
            #print(chunk, end="", flush=True)
            generated_text += text
            gen_chunk = GenerationChunk(text=text)

            if run_manager:
                run_manager.on_llm_new_token(gen_chunk.text, chunk=gen_chunk)

            yield gen_chunk

        if not len(generated_text) > 0:  # No any text generated.
            yield GenerationChunk(text="")

        if self.callback_handler:
            self.callback_handler.on_query_end(generated_text)

    def _generate(      # The 'chain' will call this function.
        self,
        prompts: list[str],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> LLMResult:
        if stop is not None:
            raise ValueError("stop kwargs are not permitted.")

        generations = []

        for prompt in prompts:
            generated_text = ""
            #print(prompt)

            if self.callback_handler:
                self.callback_handler.on_query_start(None, prompt)

            response = self.model.query(prompt, **kwargs)

            for text in response:
                #print(chunk, end="", flush=True)
                generated_text += text

                if run_manager:
                    run_manager.on_llm_new_token(text, chunk=text)
            generations.append([Generation(text=generated_text)])

            if self.callback_handler:
                self.callback_handler.on_query_end(generated_text)

        return LLMResult(generations=generations,
                        llm_output={
                            "model_name": self.model_name
                        })

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Return a dictionary of identifying parameters."""
        return {
            "model_name": self.model_name,
        }

    @property
    def _llm_type(self) -> str:
        return self.model_name

    def __del__(self):
        if hasattr(self, "model") and self.model is not None:
            self.model.exit()  # We need to exit the thread in the 'model' before delete it.
            del(self.model)
            self.model = None
        else:
            print("Failed to delete model")


###########################################################################


def main():    
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--prompt", default="hi", type=str)
    args = parser.parse_args()
    prompt = args.prompt

    print("[main ChainUtils.py]")

    prompt = "hello"
    model_name = "Qwen2.0-7B-AR32-v29"
    llm = GenieLLM() # callback_manager=callback_manager
    llm.init(model_name=model_name)

    answer = llm.invoke(prompt)
    print(answer)
 
    for chunk in llm.stream(prompt):
        print(chunk, end="", flush=True)
        pass

    run_manager = CallbackManagerForLLMRun(
        run_id="Genie_ID",
        handlers=[],
        inheritable_handlers=[]
    )
    for chunk in llm._stream(prompt, run_manager=run_manager):
        print(chunk.text, end="", flush=True)
        pass

    del(llm)
    llm = None


if __name__ == "__main__":

    main()
