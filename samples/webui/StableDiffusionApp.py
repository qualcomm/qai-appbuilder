# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import os
import sys
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "generativeai", "image_generation"))
import stable_diffusion_v2_1.stable_diffusion_v2_1 as stable_diffusion_v2_1 # We need add this line before import 'gradio'.
import gradio as gr


####################################################################

HOST="0.0.0.0"
PORT=8978

headjs = """
<script>
function dark_mode() {
  href = window.location.href
  if (!href.endsWith('?__theme=dark')) {
    window.location.replace(href + '?__theme=dark');
  }
}

function on_load() {
    dark_mode();
}

window.setTimeout(on_load, 300);
</script>
"""

css="""
body{display:flex;}

.button {
    height: 86px;
}

.gallery {
    scrollbar-width: thin;
    scrollbar-color: grey black;
}

footer{display:none !important}
"""

####################################################################

execution_ws = os.path.dirname(os.path.abspath(__file__))

user_prompt = ""
uncond_prompt = ""
user_seed = -1
user_step = 20
user_text_guidance = 7.5    # User define text guidance, any float value in [5.0, 15.0]


def modelExecuteCallback(result):
    if ((None == result) or isinstance(result, str)):   # None == Image generates failed. 'str' == image_path: generated new image path.
        if (None == result):
            result = "None"
        else:
            print("Image saved to '" + result + "'")
    else:
        result = (result + 1) * 100
        result = int(result / user_step)
        result = str(result)
        # print("modelExecuteCallback result: " + result)

def infer(text, text2, step, guidance, seed, number):
    global user_prompt, uncond_prompt, user_seed, user_step, user_text_guidance
    
    user_prompt = text
    uncond_prompt = text2
    user_step = step
    user_text_guidance = float(guidance)
    user_seed = seed
    
    image_paths = []

    for i in range(number):
        stable_diffusion_v2_1.setup_parameters(user_prompt, uncond_prompt, user_seed, user_step, user_text_guidance)
        image_path = stable_diffusion_v2_1.model_execute(modelExecuteCallback, execution_ws + "\\images", False)
        image_paths.append(image_path)

    return image_paths

####################################################################

if __name__ == '__main__':

    with gr.Blocks(fill_width=True, fill_height=True, css=css, theme=gr.themes.Glass()) as demo:
        demo.title = "文生图应用"
        gr.Markdown("<h1><center>文生图应用</center></h1>")

        with gr.Row():
            with gr.Column(scale=9, visible=True):

                text_gr = gr.Textbox(label="提示词[Prompt]", show_label=True, lines=2, max_lines=2)
                text2_gr = gr.Textbox(label="负向提示词[Negative Prompt]", show_label=True, lines=2, max_lines=2)
                
            with gr.Column(scale=1, visible=True):
                btn_gr = gr.Button("开始生图 🚀 ", elem_classes="button")

        with gr.Row():
            step_gr = gr.Slider(scale=2, label="迭代步数", step = 1, maximum = 50, minimum = 1, value = 20)
            guidance_gr = gr.Slider(scale=2, label="文本指导", step = 0.1, maximum = 15.0, minimum = 5.0, value = 7.5)
            seed_gr = gr.Number(label="随机数种子", maximum = 9999999999, minimum = -1, value = -1)
            number_gr = gr.Number(label="图片数量", maximum = 12, minimum = 1, value = 2)

        gallery_gr = gr.Gallery(columns=[6], rows=[2], show_label=False, object_fit="contain", height="auto", elem_classes="gallery")

        btn_gr.click(infer, inputs=[text_gr, text2_gr, step_gr, guidance_gr, seed_gr, number_gr], outputs=gallery_gr)

    stable_diffusion_v2_1.model_initialize()

    # Bypass system proxy for localhost so Gradio 5.x internal startup health check succeeds.
    # Gradio calls httpx.get("http://localhost:<port>/gradio_api/startup-events") after starting
    # the server; if a system proxy intercepts localhost traffic it returns 403 and Gradio aborts.
    for _env_key in ("NO_PROXY", "no_proxy"):
        _existing = os.environ.get(_env_key, "")
        _bypass = "localhost,127.0.0.1"
        if _bypass not in _existing:
            os.environ[_env_key] = (_existing + "," + _bypass).lstrip(",")

    demo.queue().launch(server_name=HOST, share=False, inbrowser=True, server_port=PORT)
