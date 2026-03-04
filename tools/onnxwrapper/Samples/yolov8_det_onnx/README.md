# yolov8_det Onnx Sample Code

## Introduction
This is onnx sample code for using AppBuilder to load yolov8_det QNN model to HTP and execute inference to predicts bounding boxes and classes of objects in an image.

## Run the following command:

If you want to run the sample code with onnx models.
python prepare_yolov8_det_onnx_models.py
python yolov8_det_onnx_inference.py --model models-onnx\yolov8n.onnx

If you want to run the sample code with qnn models.
python prepare_yolov8_det_qnn_models.py
python onnxexec.py yolov8_det_onnx_inference.py --model models-qnn\yolov8_det.bin

You also can add the following code at beginning of yolov8_det_onnx_inference.py.
from qai_appbuilder import onnxwrapper
Then run the following command.
python yolov8_det_onnx_inference.py --model models-qnn\yolov8_det.bin

## Output
The output image will be saved to output.jpg
