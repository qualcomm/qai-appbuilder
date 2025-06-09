# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

# python GenieAPIClientImage.py --prompt "A little cat is playing on the grass near river."

import argparse
from openai import OpenAI
from PIL import Image
import os
import base64

HOST="localhost"
PORT="8910"

parser = argparse.ArgumentParser()
parser.add_argument("--stream", action="store_true")
parser.add_argument("--prompt", default="A futuristic cityscape at sunset", type=str)
args = parser.parse_args()

client = OpenAI(base_url="http://" + HOST + ":" + PORT + "/v1", api_key="123")

generation_response = client.images.generate(
    model = "Stable Diffusion 3",
    prompt=args.prompt,
    n=1,
    size="512x512",
    response_format="url",
)

# print response
# print(generation_response)
# print(generation_response.data[0])

image_data = generation_response.data[0].b64_json
if image_data:
    image_data = base64.b64decode(image_data)

    image_path = "images"
    if not os.path.exists(image_path):
        os.makedirs(image_path)
    
    image_file = image_path + "//image.png"
    with open(image_file, mode="wb") as png:
        png.write(image_data)

    image = Image.open(image_file)
    image.show()
