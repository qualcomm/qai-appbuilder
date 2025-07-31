# RagSaveDoc guide

RagSaveDoc is a RAG tool which is used to embed .pdf/.txt documents to vector database.

## Environment setup

Step 1. Install python 3.12 or above.<br>
Step 2. Install necessary python module. Recommend to setup python virtual environment and intall modules with below commands.<br>
```
python -m venv myfletenv
myfletenv\Scripts\activate
pip install flet asyncio langchain langchain_community langchain-huggingface langchain-chroma jieba openai PyPDF2 rank_bm25 huggingface_hub hf_xet python-dateutil llama-cpp-python PyMuPDF pyyaml
```

## How to run in python environment

Step 1. Start cmd command prompt window and active python virtual environment with below command:
```
<myfletenv path>\Scripts\activate
```
Step 2. Go to ai-engine-direct-helper samples path and start ragsavedoc application with below command:
```
cd <your path>\ai-engine-direct-helper-2.34\samples
python fletui\GenieFletUI\windows\Ragtool\RagSaveDoc.py
```

## How to build ragsavedoc to Windows .exe file.

ragsavedoc can be built to Windows .exe file so that it can run without Python environment.<br>
Step 1. Start cmd command prompt window and active python virtual environment with below command:
```
<myfletenv path>\Scripts\activate
```
Step 2. Go to ai-engine-direct-helper ragsavedoc path and generate building .spec file with below command:
```
cd <your path>\ai-engine-direct-helper-2.34\samples\fletui\GenieFletUI\windows\Ragtool
python RagSaveDoc_generate_spec.py
```
Step 3. Build ragsavedoc with below command:
```
pyinstaller RagSaveDoc.spec
```
RagSaveDoc will be saved at <your path>\ai-engine-direct-helper-2.34\samples\fletui\GenieFletUI\windows\Ragtool\dist <br>

## Note

 - RagSaveDoc supports embedding model auto detection and downloading. So, first launch time may be longer because it will download embedding model. The following launch will be faster.
 - Saving a document which includes many pages may take long time. Application will show complete when saving done. Please be patient.
 
 - ![image-RagSaveDoc_PrintScreen](assets/RagSaveDoc_PrintScreen.jpg)