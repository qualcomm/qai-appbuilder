from openai import OpenAI

HOST = "localhost"
PORT = "8910"

client = OpenAI(base_url=f"http://{HOST}:{PORT}/v1", api_key="123")

def generate_response(prompt: str, model: str = "Qwen2.0-7B-SSD", stream: bool = False, is_debug: bool = False) -> str:
    messages = [
        {"role": "system", "content": "You are an English writer."},
        {"role": "user", "content": prompt}
    ]

    extra_body = {
        "size": 4096,
        "seed": 146,
        "temp": 1.5,
        "top_k": 13,
        "top_p": 0.6
    }

    if stream:
        if is_debug:
            print("Streaming response:")
        output = ""
        response = client.chat.completions.create(model=model, messages=messages, stream=True, extra_body=extra_body)
        for chunk in response:
            content = chunk.choices[0].delta.content
            if content:
                print(content, end="", flush=True)
                output += content
        if is_debug:
            print()
        return output
    else:
        response = client.chat.completions.create(model=model, messages=messages, extra_body=extra_body)
        return response.choices[0].message.content.strip()
