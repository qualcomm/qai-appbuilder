# real_esrgan_x4plus onnx Sample Code

## Introduction
This is onnx sample code for using AppBuilder to load real_esrgan_x4plus QNN model to HTP and execute inference to generate image. 

## Run the sample code
If you want to run the sample code with onnx models.
```bash
python prepare_real_esrgan_x4plus_onnx_models.py
python real_esrgan_x4plus_onnx_inference.py --model models-onnx\real_esrgan_x4plus-onnx-float\real_esrgan_x4plus.onnx
```

If you want to run the sample code with qnn models.
1. Run the following command.
```bash
   python prepare_real_esrgan_x4plus_qnn_models.py
```
2. Add the following code at beginning of real_esrgan_x4plus_onnx_inference.py.
```python
   from qai_appbuilder import onnxwrapper
```
3. Then run the following command.
```bash
   python real_esrgan_x4plus_onnx_inference.py --model models-qnn\real_esrgan_x4plus.bin --tile 512
```
## Output
The output image will be saved to output_x4.png

