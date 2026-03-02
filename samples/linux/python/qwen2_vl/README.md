## Qwen2-VL Demo (Linux Python)

### How to run
1. Prepare the Qwen2-VL 2B model.
	 - Follow the tutorial to quantize and convert the model:
		 https://qpm.qualcomm.com/#/main/tools/details/Tutorial_for_Qwen2_VL_2b_IoT
	 - Or download a pre-quantized QNN-format model:
		 https://www.aidevhome.com/data/models/

2. Launch the demo:

```bash
python demo_app.py <model_path>
```

Replace `<model_path>` with the directory containing the converted QNN model files.
