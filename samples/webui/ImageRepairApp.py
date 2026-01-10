# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import os
import sys
if sys.platform.startswith('linux'):
    sys.path.append(".")
    sys.path.append("linux/python")
else:
    sys.path.append(".")
    sys.path.append("python")

import numpy as np
from PIL import Image
import datetime
from tkinter import filedialog, Tk
import shutil
import real_esrgan_x4plus.real_esrgan_x4plus as real_esrgan # We need add this line before import 'gradio'.
import gradio as gr


####################################################################

HOST="0.0.0.0"
PORT=8977

IMAGE_PATH = "images/"
IMAGE_OLD = IMAGE_PATH + "old.jpeg"
IMAGE_NEW = IMAGE_PATH + "new.jpeg"

IMAGE_CONTAINER_SIZE = "680"

g_folder_path = None
g_image_name = ""

if not os.path.exists(IMAGE_PATH):
    os.makedirs(IMAGE_PATH, exist_ok=True)

if os.path.exists(IMAGE_OLD):
    os.remove(IMAGE_OLD)

if os.path.exists(IMAGE_NEW):
    os.remove(IMAGE_NEW)

####################################################################


headjs = """
<style>
.image_container {
  position: relative;
  width: IMAGE_CONTAINER_SIZEpx;
  height: IMAGE_CONTAINER_SIZEpx;
  border: 2px solid white;
  visibility: hidden;
}

.old-img {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background-size: IMAGE_CONTAINER_SIZEpx 100%;
  display:flex;
  background-repeat:no-repeat;
  background-position:center center；
}

.new-img {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background-size: IMAGE_CONTAINER_SIZEpx 100%;
  display:flex;
  background-repeat:no-repeat;
  background-position:center center；
}

.slider {
  display: flex;
  justify-content: center;
  align-items: center;
  position: absolute;
  -webkit-appearance: none;
  appearance: none;
  width: 100%;
  height: 100%;
  background: rgba(235, 235, 235, 0.3);
  outline: none;
  margin: 0;
  transition: all 0.2s;
}

.slider:hover {
  background: rgba(235, 235, 235, 0.1);
}

.slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 2px;
  height: IMAGE_CONTAINER_SIZEpx;
  background: white;
  cursor: pointer;
}

.slider-button {
  pointer-events: none;
  position: absolute;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  background-color: white;
  left: calc(50% - 14px);
  top: calc(50% - 14px);
  display: flex;
  justify-content: center;
  align-items: center;
}

.slider-button::after {
  content: "";
  padding: 3px;
  display: inline-block;
  border: solid #5e5e5e;
  border-width: 0 2px 2px 0;
  transform: rotate(-45deg);
}

.slider-button::before {
  content: "";
  padding: 3px;
  display: inline-block;
  border: solid #5e5e5e;
  border-width: 0 2px 2px 0;
  transform: rotate(135deg);
}
</style>

<style data="slider_style" type="text/css"></style>

<script>
let sliderPos = 50;
let imageWidth = IMAGE_CONTAINER_SIZE;
let imageHeight = IMAGE_CONTAINER_SIZE;

function slide_pos() {
    var slider_style = document.querySelector('[data="slider_style"]');
    var height = document.documentElement.clientHeight - 60;

    var image_info = document.getElementById("image_info")
    if (image_info == null) {
        document.querySelector(".slider-button").style.left = `calc(${sliderPos}% - 14px)`;
        slider_style.innerHTML = `.slider::-webkit-slider-thumb { height: ${height}px !important; }`;

        document.querySelector(".image_container").style.height = height + "px";
        document.querySelector(".new-img").style.backgroundSize = imageWidth + "px " + height + "px";
        document.querySelector(".old-img").style.backgroundSize = imageWidth + "px " + height + "px";
        document.querySelector(".help_container").style.visibility = "visible";
        return;
    }

    var image_width = image_info.dataset.w;
    var image_height = image_info.dataset.h;

    var content_div = document.getElementById("html_container")
    var width = content_div.offsetWidth;
    // console.log(width + "/" + height);

    w_ratio = width / image_width;
    h_ratio = height / image_height;
    scale = 1;    
    if (w_ratio > h_ratio) {
        scale = h_ratio;
    }
    else {
        scale = w_ratio;
    }

    var new_width = scale * image_width;
    var new_height = scale * image_height;
    // console.log(new_width + "/" + new_height + "/" + scale);

    imageWidth = new_width;
    imageHeight = new_height;

    document.querySelector(".image_container").style.visibility = "visible";
    document.querySelector(".help_container").style.visibility = "hidden";

    document.querySelector(".html_container").style.height = height + "px";

    document.querySelector(".image_container").style.width = new_width + "px";
    document.querySelector(".image_container").style.height = new_height + "px";
    document.querySelector(".new-img").style.backgroundSize = new_width + "px " + new_height + "px";
    document.querySelector(".old-img").style.backgroundSize = new_width + "px " + new_height + "px";
    
    document.querySelector(".new-img").style.width = `${sliderPos}%`;
    document.querySelector(".slider-button").style.left = `calc(${sliderPos}% - 14px)`;

    slider_style.innerHTML = `.slider::-webkit-slider-thumb { height: ${imageHeight}px !important; }`;
}

function slide_move() {
    const slider = document.getElementById("slider");

    slider.addEventListener("input", function(e) {
      sliderPos = e.target.value / 100;
      slide_pos();
    });
}

function dark_mode() {
  href = window.location.href
  if (!href.endsWith('?__theme=dark')) {
    window.location.replace(href + '?__theme=dark');
  }
}

function on_load() {
    dark_mode();

    var targetNode = document.getElementById("html_change");
    targetNode.style.visibility = "hidden";
    document.querySelector("#html_change").style.visibility = "hidden";
    var config = { attributes: true, childList: true, subtree: true };    

    var callback = function(mutationsList) {
        mutationsList.forEach(function(item,index){
            if (item.type == 'childList') {
                text = item.target.innerHTML;
                if (text) {
                    if (text.includes("###update###")) {
                        console.log('nodes change...');

                        var no = new Date().getTime();                        
                        new_image_url = '/gradio_api/file=IMAGE_NEW?no=' + no
                        old_image_url = '/gradio_api/file=IMAGE_OLD?no=' + no

                        document.getElementById('old-img').style.backgroundImage="url(" + old_image_url + ")";
                        document.getElementById('new-img').style.backgroundImage="url(" + new_image_url + ")";
                        slide_pos()
                    }
                }
            } else if (item.type == 'attributes') {
            }
        });
    };

    var observer = new MutationObserver(callback);
    observer.observe(targetNode, config);
    slide_move();
    slide_pos();
}

window.setTimeout(on_load, 300);

</script>
"""

css="""
body{display:flex;} 
.button {
    width: 256px;
}
footer{display:none !important}
"""


headjs = headjs.replace("IMAGE_CONTAINER_SIZE", IMAGE_CONTAINER_SIZE)
headjs = headjs.replace("IMAGE_NEW", IMAGE_NEW)
headjs = headjs.replace("IMAGE_OLD", IMAGE_OLD)


####################################################################


def html_update(size):
    now = datetime.datetime.now()
    time_str = now.strftime("%Y_%m_%d_%H_%M_%S")
    html_content = f"<div data-w='{size[0]}' data-h='{size[1]}' id='image_info' style='display: none'>###update###='{time_str}'</div>"

    return html_content

def image_uploaded(img_path):
    global g_image_name
    
    pil_old = Image.open(img_path)
    size = pil_old.size
    pil_old.save(IMAGE_OLD)

    if os.path.exists(IMAGE_NEW): # remove image.
        os.remove(IMAGE_NEW)

    g_image_name = os.path.basename(img_path)
    #print(g_image_name)

    return html_update(size)

def directory_select():
    global g_folder_path

    try:
        root = Tk()
        root.withdraw()
        root.wm_attributes("-topmost", 1)
        g_folder_path = filedialog.askdirectory(initialdir=g_folder_path or ".")
        root.destroy()
        # print(g_folder_path)
    except Exception as e:
        raise RuntimeError(f"打开文件对话框异常: {e}") from e

def image_save():
    global g_folder_path

    if not os.path.exists(IMAGE_NEW):
        gr.Warning("请先修复图片再保存！", duration=3)
        return

    try:
        root = Tk()
        root.withdraw()
        root.wm_attributes("-topmost", 1)
        image_path = filedialog.asksaveasfilename(title=u'保存文件', defaultextension='.jpeg', 
                                                  filetypes=[("JPEG", "*.jpg *.jpeg"), ("PNG", "*.png"), ('All Files', '*.*')],
                                                  initialdir=g_folder_path or ".", initialfile=g_image_name)
        root.destroy()
        if image_path:
            g_folder_path = os.path.dirname(image_path)
            shutil.copy(IMAGE_NEW, image_path)

    except Exception as e:
        raise RuntimeError(f"打开文件对话框异常: {e}") from e

def image_repair():
    if os.path.exists(IMAGE_OLD):
        real_esrgan.Inference(IMAGE_OLD, IMAGE_NEW, False)
        pil_new = Image.open(IMAGE_NEW)
        size = pil_new.size
        pil_new = np.array(pil_new)
        return html_update(size), pil_new
    else:
        gr.Warning("请先选择要修复的图片！", duration=3)

    return "", None


####################################################################


if __name__ == '__main__':

    with gr.Blocks(head=headjs, css=css, theme=gr.themes.Glass(), fill_width=True, fill_height=True) as demo:
        demo.title = "Image Repair App"
        #gr.HTML("""<h1 align="center">Image Repair App</h1>""")
        gr.set_static_paths(paths=[IMAGE_PATH])

        image_gr = None
        reapir_gr = None

        with gr.Row():
            with gr.Column(scale=1):
                with gr.Tab("Image Repair"):
                    with gr.Row():
                        with gr.Column(scale=1, visible=True):
                            image_gr = gr.Image(type="filepath", sources=['upload', 'clipboard', 'webcam'], width=256, height=256, elem_classes="radio-group", format="jpeg",
                                                label="Select Image", scale=1, interactive=True, show_label=True, show_download_button=True)

                            #outpath_gr = gr.Button("Output Folder", elem_classes="button")
                            #outpath_gr.click(directory_select)

                            reapir_gr = gr.Button("Repair Picture 🚀️ ", elem_classes="button")

                            imagesave_gr = gr.Button("Save Picture 💿 ", elem_classes="button")
                            imagesave_gr.click(image_save)

                    with gr.Row():
                        html_change_gr = gr.HTML(elem_id="html_change", visible=True)

                #with gr.Tab("参数设置"):
                #    with gr.Column(scale=1, visible=True):
                #        max_gr = gr.Slider(1, 12, value=2, step=1.0, label="test", interactive=True)                                            

            with gr.Column(scale=9):
                with gr.Row():
                    with gr.Column(scale=2):
                        html_container = f"""
                        <div class="html_container" id="html_container" style='border: 1px; justify-content: center; display:flex; align-items: center; border-radius:20px; background-color: #2E2E2E;'>
                          <div class='image_container' style='border: 1px; background-color: #2E2E2E;'>
                            <div class='old-img' id='old-img'></div>
                            <div class='new-img' id='new-img'></div>
                            <input type="range" min="1" max="10000" value="5000" class="slider" name='slider' id="slider">
                            <div class='slider-button'></div>
                          </div>
                          <div class='help_container' style='visibility: hidden; border: 1px; position: absolute; top: 40%; left: 36%; width: 28%; height: 200px; justify-content: center; display:flex; align-items: center; border-radius:20px; background-color: #4E494B;'>
                            <div style='justify-content: center; align-items: center; text-align: center; color: white; margin: 10px; padding: 5px;'>
                              <h3 align="center" style="color: white">选择要修复的图片</h3><br>
                              在左边选择PNG、JPEG或其他格式的图片，点击“修复图片”按钮进行修复。图片可以选自本地文件夹、剪切版，或者通过电脑摄像头直接拍摄。<br><br><br>
                              <div align="center" style='border: 1px; margin: 0 auto; width: 180px; height: 30px; padding-top: 5px; border-radius:15px; background-color: #2E2E2E;'>
                              图片修复应用 v1.0.0
                              </div>
                              <br>
                            </div>
                          </div>
                        </div>
                        """

                        gr.HTML(html_container)

            image_gr.upload(image_uploaded, inputs=image_gr, outputs=[html_change_gr])
            reapir_gr.click(image_repair, outputs=[html_change_gr, image_gr])


    real_esrgan.Init()

    demo.queue().launch(server_name=HOST, share=False, inbrowser=True, server_port=PORT)
