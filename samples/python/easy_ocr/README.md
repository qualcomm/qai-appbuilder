# easyocr Sample Code

## Introduction
This is sample code for using AppBuilder to load easyocr QNN model to HTP and execute optical character recognition (OCR) inference to detect and extract text from input images.

###
cd ai-engine-direct-helper\samples
python python\easyocr\easy_ocr.py

####Resources:
Char folder: holds the charactors that can be displayed, in different encoding.
models folder: Downloaded model binaries.
simsun.ttc: Font to draw Chinese. (You can use other true type font.)
english.png: Default image to be used. (You can use any image with resolution 800*608)
Other PNG files: other sample images to try with. Use "demo_local.py --Image_Path" with full image file path to try other images.