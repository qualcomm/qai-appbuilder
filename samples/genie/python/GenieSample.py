import os
from qai_appbuilder import (GenieContext)

lib_path = "qai_libs"  # The QAIRT runtime libraries path.
if not lib_path in os.getenv('PATH'):
    lib_path = os.getenv('PATH') + ";" + lib_path + ";"
    os.environ['PATH'] = lib_path

def response(text):
    # Print model generated text.
    print(text, end='', flush=True)
    return True

# Initialize the model.
config = os.path.join("genie", "python", "models", "IBM-Granite-v3.1-8B", "config.json")
dialog = GenieContext(config)

# Ask question.
prompt = "<|start_of_role|>system<|end_of_role|>You are a helpful assistant.<|end_of_text|> <|start_of_role|>user<|end_of_role|>How to fish?<|end_of_text|> <|start_of_role|>assistant<|end_of_role|>"
dialog.Query(prompt, response)
