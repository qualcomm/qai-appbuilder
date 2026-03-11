## Qwen2-VL Demo (Linux Python)

Qwen2-VL Demo is a VLM (Vision Language Model) demonstration program implemented using the QAI Appbuilder Python interface. It supports understanding images, videos, and camera feeds. Currently, the supported VLM model is Qwen2-VL.

<img src="./vlm_demo.png" alt="VLM Demo Screen"  >

### How to run

1. Prepare the Qwen2-VL 2B model.

	 - Follow the tutorial to quantize and convert the model:
		 https://qpm.qualcomm.com/#/main/tools/details/Tutorial_for_Qwen2_VL_2b_IoT
	 - Or download a pre-quantized QNN-format model (recommond):
	```bash
	 wget https://www.aidevhome.com/data/adh2/models/suggested/qwen2vl2b.zip

	 unzip qwen2vl2b.zip
	```
2. Run the following commands to install the required Python packages:

   ```bash
   # Install core Python packages
   pip install requests==2.32.3 \
            py3-wget==1.0.12 \
            tqdm==4.67.1 \
            importlib-metadata==8.5.0 \
            qai-hub==0.30.0 \
            opencv-python==4.10.0.82 \
            gradio

   pip install transformers==4.45.0 torch==2.9.1
   pip install torchvision>=0.9.0
   pip install qwen-vl-utils
   ```

3. Set up environment variables

   ```bash
   export QNN_SDK_ROOT=/path/to/qnn/sdk
   export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2
   export ADSP_LIBRARY_PATH=$QNN_SDK_ROOT/lib/hexagon-v73/unsigned
   export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libtbb.so.12
   ```

   **Note:** Replace `/path/to/qnn/sdk` with your actual QNN SDK installation path.



#### 3. Launch the Demo

   Run the demo application with the model path:

   ```bash
   python demo_app.py <model_path>
   ```

   **Parameters:**
   - `<model_path>`: Path to the directory containing the converted QNN model files from Step 1.