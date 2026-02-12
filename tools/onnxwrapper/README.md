# onnx runtime to qnn runtime

## Introduction
If you have onnx sample codes and onnx models, you can convert the models to QNN models or download the corresponding QNN models from aihub then run the onnx sample codes as following steps.
1. Copy onnxexec.py and onnxwrapper.py from ai-engine-direct-helper\tools\onnxwrapper to your onnx sample codes folder.
2. Run python onnxexec.py your_onnx_sample.py
or add the following codes in your onnx sample code at the beginning.

    from qai_appbuilder import onnxwrapper
    
run python your_onnx_sample.py
