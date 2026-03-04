# facemap_3dmm onnx Sample Code

## Introduction
This is onnx sample code for using QAI AppBuilder to load facemap_3dmm QNN model to HTP and execute 3D face reconstruction inference to generate parametric 3D face models from 2D facial images. The facemap_3dmm_onnx_infer.py is onnx sample code.

## Run the sample code

```
If you want to run the sample code with onnx models.
python prepare_facemap_3dmm_onnx_models.py
python facemap_3dmm_onnx_infer.py --model models-onnx\facemap_3dmm-onnx-float\facemap_3dmm.onnx --image input.jpg

If you want to run the sample code with qnn models.
python prepare_facemap_3dmm_qnn_models.py
python onnxexec.py facemap_3dmm_onnx_infer.py --model models-qnn\facemap_3dmm.bin --image input.jpg

You also can add the following code at beginning of facemap_3dmm_onnx_infer.py.
from qai_appbuilder import onnxwrapper
Then run the following command.
python facemap_3dmm_onnx_infer.py --model models-qnn\facemap_3dmm.bin --image input.jpg

```
## Output
You can see output.jpg in out folder.