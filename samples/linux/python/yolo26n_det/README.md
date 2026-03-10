# yolo26n_det Sample Code

## Introduction
This is sample code for using AppBuilder to load yolo26n_det QNN model to HTP and execute inference to predicts bounding boxes and classes of objects in an image. The yolo26n_det.py file corresponds to the qai_appbuilder version. Additionly, we provide both the ultralytics version and onnxruntime version scripts. The ultralytics version script can be used to export an .onnx model.

## How to get the QNN format model
### 1. Export .onnx format model using ultralytics version script
```bash
python ultralytics_version.py
```

### 2. Set QNN related Environment Variables:
```bash
export QNN_SDK_ROOT=<path_to_your_qnn_sdk_root>
export LD_LIBRARY_PATH=$QNN_SDK_ROOT/lib/aarch64-oe-linux-gcc11.2:$LD_LIBRARY_PATH
export PYTHONPATH=${QNN_SDK_ROOT}/lib/python
export PATH=${QNN_SDK_ROOT}/bin/x86_64-linux-clang/:$PATH
export CPLUS_INCLUDE_PATH=/usr/include/c++/9:/usr/include/x86_64-linux-gnu/c++/9
export LIBRARY_PATH=/usr/lib/gcc/x86_64-linux-gnu/9
```

### 3. Model Conversion
```bash
$QNN_SDK_ROOT/bin/x86_64-linux-clang/qnn-onnx-converter \
    -i ./yolo26n.onnx \
    --preserve_io layout \
    --preserve_io datatype \
    --output_path ./yolo26n.cpp
```

### 4. Model Lib Generation
```bash
$QNN_SDK_ROOT/bin/x86_64-linux-clang/qnn-model-lib-generator \
    -c ./yolo26n.cpp \
    -b ./yolo26n.bin \
    -t aarch64-oe-linux-gcc11.2 \
    -o ./
```
At this stage, a .so model library is generated. This library can be used on both the QNN CPU and HTP backends.

### 5. Context Binary Generation
```bash
$QNN_SDK_ROOT/bin/x86_64-linux-clang/qnn-context-binary-generator \
    --model ./x86_64-linux-clang/libyolo26n.so \
    --soc_model 77 \
    --backend ${QNN_SDK_ROOT}/lib/x86_64-linux-clang/libQnnHtp.so \
    --binary_file cntx_yolo26n_soc77_fp16
```
At this stage, a context binary (.bin) is generated, which can only be used on the HTP backend.

For more information, please refer to the QAIRT SDK documentation.

## Notes
- When running the `yolo26n_det/yolo26n_det.py` script, make sure to replace the `model_path` with your own model path generated in the previous step.

## References
[1] https://docs.qualcomm.com/nav/home/general_tools.html?product=1601111740009302

[2] https://docs.ultralytics.com/models/yolo26/