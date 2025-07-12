# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import os
import sys
sys.path.append(".")
sys.path.append("python")
import utils.install as install
import argparse
from transformers import AutoTokenizer
from typing import cast
import torch
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

####################################################################

MODEL_ID = "mn03prw8n"
MODEL_NAME = "nomic_embed_text"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
SEQ_LEN = 128
####################################################################

execution_ws = os.getcwd()
qnn_dir = execution_ws + "\\qai_libs"

if not "python" in execution_ws:
    execution_ws = execution_ws + "\\" + "python"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "\\" + MODEL_NAME

model_dir = execution_ws + "\\models"
model_path = model_dir + "\\" + MODEL_NAME + ".bin"

####################################################################

nomic_embed_text=None
tokenizer = None

# NomicEmbedText class which inherited from the class QNNContext.
class NomicEmbedText(QNNContext):
    def Inference(self, input_ids, attention_mask):
        input_datas=[input_ids, attention_mask]
        output_data = super().Inference(input_datas)[0]
        return output_data



def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, model_path, desc=desc, fail=fail)

    if not ret:
        exit()


def Init():
    global nomic_embed_text,tokenizer

    model_download()

    tokenizer= AutoTokenizer.from_pretrained(
            "bert-base-uncased", model_max_length=SEQ_LEN
    )

    # Config AppBuilder environment.
    QNNConfig.Config(qnn_dir, Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for nomic_embed_text objects.
    nomic_embed_text = NomicEmbedText("nomic_embed_text", model_path)


def preprocess_text(input_text):
    inputs =tokenizer(input_text, padding="max_length", return_tensors="pt")
    input_ids = cast(torch.Tensor, inputs["input_ids"].to(torch.int32))
    attention_mask = cast(torch.Tensor, inputs["attention_mask"].to(torch.int32))

    return input_ids,attention_mask

    
def Inference(input_text):
    #Preprocess the text.
    input_ids,attention_mask = preprocess_text(input_text)

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_embeddings = nomic_embed_text.Inference(input_ids, attention_mask)

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
    
    # show the Embeddings
    print(f"Embeddings: \n{output_embeddings}")


def Release():
    global nomic_embed_text

    # Release the resources.
    del(nomic_embed_text)


def main(input = None):

    if input is None:
        input = "hello!"

    Init()

    Inference(input)

    Release()



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Encode Text.")
    parser.add_argument('--text', help='Text to encode', default=None)
    args = parser.parse_args()

    main(args.text)


