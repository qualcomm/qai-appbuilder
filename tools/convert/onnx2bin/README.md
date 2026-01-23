## Convert ONNX model to QNN(*.bin) format:

### Prepare files and put them to same directory, for example C:\ONNX2BIN.
### ONNX modle files. 
Take real_esrgan_x4plus as an example, you can download real_esrgan_x4plus-w8a8.onnx and real_esrgan_x4plus-float.onnx from [Real-ESRGAN-x4plus](https://aihub.qualcomm.com/compute/models/real_esrgan_x4plus). Put the onnx file and its model.data file at the same directory.

### For WoS Platform:
• raw file
You can run below command to generate random512_512_3.raw file:
```
python generate_random_hwc.py -H 512 -W 512 -C 3 -O .
```
• input_list file
Create input_list.txt in which includes above raw file:
```
random512_512_3.raw
```

• [htp backend extension json file](https://docs.qualcomm.com/bundle/publicresource/topics/80-63442-10/htp_backend.html#qnn-htp-backend-extensions)
htp_backend_ext_fp16_v73-real_esrgan_x4plus.json

### For Android Platform:
• raw file
You can run below command to generate random128_128_3.raw file:
```
python generate_random_hwc.py -H 128 -W 128 -C 3 -O .
```
• input_list file
Create input_list-128.txt in which includes above raw file:
```
random128_128_3.raw
```
*Note: To improve accuracy, please do not use the above random raw file. Instead, use your actual input data or images to generate the new raw file.<br>

• htp_backend_ext_fp16_v79-real_esrgan_x4plus.json<br>


### Install QAIRT SDK:
Download [QAIRT SDK](https://softwarecenter.qualcomm.com/api/download/software/sdks/Qualcomm_AI_Runtime_Community/All/2.37.1.250807/v2.37.1.250807.zip) and unzip it to: C:\Qualcomm\AIStack\QAIRT\2.37.1.250807\
Copy this file C:\Qualcomm\AIStack\QAIRT\2.37.1.250807\lib\x86_64-windows-msvc\QnnHtp.dll to C:\ONNX2BIN.

## [Windows Setup](https://docs.qualcomm.com/bundle/publicresource/topics/80-63442-10/windows_setup.html)
Please Install and use python 3.10, not use python 3.12.

Then run below commands in PowerShell:
```
$env:QNN_SDK_ROOT = "C:\Qualcomm\AIStack\QAIRT\2.37.1.250807"
cd C:\Qualcomm\AIStack\QAIRT\2.37.1.250807\bin

- After install visual studio, CLang and Cmake, then set path:
set CMake_Path="C:\Program Files\CMake\bin\"
set CLang_Path="C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\Llvm\bin"

Unblock-File ./envsetup.ps1
./envsetup.ps1

Unblock-File "${QNN_SDK_ROOT}/bin/check-windows-dependency.ps1"
& "${QNN_SDK_ROOT}/bin/check-windows-dependency.ps1"

Unblock-File "${QNN_SDK_ROOT}/bin/envcheck.ps1"
& "${QNN_SDK_ROOT}/bin/envcheck.ps1" -m

py -3.10 -m venv "venv"
& "venv\Scripts\Activate.ps1"

python "$env:QNN_SDK_ROOT\bin\check-python-dependency"
```

## Convert Commands
Switch to your directory which includes above prepared files, then execute 3 APIs(qnn-onnx-converter, qnn-model-lib-generator and qnn-onnx-converter) to get QNN(*.bin) format model file.
```
cd C:\ONNX2BIN
```
### convert real_esrgan_x4plus-float.onnx for wos(Snapdragon® X Elite Devices)
```
python ${QNN_SDK_ROOT}/bin/x86_64-windows-msvc/qnn-onnx-converter --input_network .\models\real_esrgan_x4plus-float\model.onnx --input_dim image 1,3,128,128 --output_path real_esrgan_x4plus-float.cpp --input_list input_list-128.txt --act_bitwidth 16 --weights_bitwidth 8

python ${QNN_SDK_ROOT}\bin\x86_64-windows-msvc\qnn-model-lib-generator -c real_esrgan_x4plus-float.cpp -b real_esrgan_x4plus-float.bin -o output -t windows-x86_64

& "$env:QNN_SDK_ROOT\bin\x86_64-windows-msvc\qnn-context-binary-generator.exe" --backend QnnHtp.dll --model output\x64\real_esrgan_x4plus-float.dll --output_dir output --config_file config\htp_backend_ext-fp16-v73-real_esrgan_x4plus.json --binary_file real_esrgan_x4plus-fp16-v73
```

### convert real_esrgan_x4plus-w8a8.onnx for android mobile device(Snapdragon® 8 Elite Gen 5):
```
python ${QNN_SDK_ROOT}/bin/x86_64-windows-msvc/qnn-onnx-converter --input_network .\models\real_esrgan_x4plus-w8a8\model.onnx --input_dim image 1,3,128,128 --output_path real_esrgan_x4plus-w8a8.cpp --input_list input_list-128.txt --act_bitwidth 8 --weights_bitwidth 8 --bias_bw 8

python ${QNN_SDK_ROOT}\bin\x86_64-windows-msvc\qnn-model-lib-generator -c real_esrgan_x4plus-w8a8.cpp -b real_esrgan_x4plus-w8a8.bin -o output -t windows-x86_64

& "$env:QNN_SDK_ROOT\bin\x86_64-windows-msvc\qnn-context-binary-generator.exe" --backend QnnHtp.dll --model output\x64\real_esrgan_x4plus-w8a8.dll --output_dir output --config_file config\htp_backend_ext-w8a8-v81-real_esrgan_x4plus.json --binary_file real_esrgan_x4plus-w8a8-v81
```
