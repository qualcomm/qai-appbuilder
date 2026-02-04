import argparse
import base64
import requests
import os
from openai import OpenAI

IP_ADDR = "127.0.0.1:8910"

parser = argparse.ArgumentParser()
parser.add_argument("--stream", action="store_true", dest="stream", help="Answer in stream mode")
parser.set_defaults(stream=True)

parser.add_argument("--prompt",
                    type=str,
                    help="Your questions for ask. Default is `hello`",
                    default="hello")

parser.add_argument("--system",
                    type=str,
                    help="Model system prompt. Default is `You are a helpful assistant.`",
                    default="You are a helpful assistant.")

parser.add_argument("--img",
                    type=str,
                    help="Path to local image file or Image URL",
                    default="")

parser.add_argument("--audio",
                    type=str,
                    help="Path to local audio file or audio URL",
                    default="")

parser.add_argument("--ip",
                    type=str,
                    help="The server ip for communication. Default is `127.0.0.1`",
                    default="127.0.0.1")

parser.add_argument("--model",
                    type=str,
                    help="The model will be loaded. It's should located at ./models/xxxx",
                    default="127.0.0.1")
args = parser.parse_args()


def encode_image(image_input):
    if image_input.startswith(('http://', 'https://')):
        try:
            print(f"Downloading image from URL: {image_input}...")
            response = requests.get(image_input, timeout=10)
            response.raise_for_status()
            return base64.b64encode(response.content).decode('utf-8')
        except Exception as e:
            raise Exception(f"Failed to download image from URL: {e}")

    else:
        try:
            if not os.path.exists(image_input):
                raise FileNotFoundError(f"Local file not found: {image_input}")

            with open(image_input, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode('utf-8')
        except Exception as e:
            raise Exception(f"Failed to load local image: {e}")


base64_image = ""
try:
    if args.img:
        base64_image = encode_image(args.img)
except Exception as e:
    print(f"Error: {e}")
    exit(1)

client = OpenAI(base_url="http://" + IP_ADDR + "/v1", api_key="123")

custom_messages = [
    {"role": "system", "content": args.system},
    {
        "role": "user",
        "content": {
            "question": args.prompt,
            "image": base64_image,
            "audio": args.audio
        }
    }
]

body = {
    "size": 4096,
    "seed": 146,
    "temp": 1.5,
    "top_k": 13,
    "top_p": 0.6,
    "messages": custom_messages
}

# send the request
try:
    if args.stream:
        response = client.chat.completions.create(
            model=args.model,
            stream=args.stream,
            messages="",
            extra_body=body
        )
        for chunk in response:
            if chunk.choices:
                content = chunk.choices[0].delta.content
                if content is not None:
                    print(content, end="", flush=True)
        print()
    else:
        response = client.chat.completions.create(
            model=args.model,
            stream=args.stream,
            messages="",
            extra_body=body
        )
        if response.choices:
            print(response.choices[0].message.content)

except Exception as e:
    print(f"\nRequest failed: {e}")
