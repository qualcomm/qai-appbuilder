# stable_diffusion_v1_5 Sample Code

## Introduction
This is sample code for using AppBuilder to load Stable Diffusion 1.5 QNN models to HTP and execute inference to generate image.

## Run the sample code
```
If you want to run the sample code with onnx models.
python prepeare_stable_diffusion_onnx_models.py
python stable_diffusion_1_5_onnx_infer.py --model_root models-onnx\modularai_stable-diffusion-1-5-onnx --provider cpu --out sd15_out.png

If you want to run the sample code with qnn models.
python prepeare_stable_diffusion_qnn_models.py
python onnxexec.py stable_diffusion_1_5_onnx_infer.py --model_root models-qnn --vae_scale 1.0

You also can add the following code at beginning of stable_diffusion_1_5_onnx_infer.py.
from qai_appbuilder import onnxwrapper
Then run the following command.
python stable_diffusion_1_5_onnx_infer.py --model_root models-qnn --vae_scale 1.0

```
## Output
The output image will be saved to sd15_out.png

