# whisper_base_en onnx Sample Code

## Introduction
This is onnx sample code for using AppBuilder to load whisper_base_en QNN model to HTP and execute inference. 

## Run the sample code
If you want to run the sample code with onnx models.
```bash
python prepare_whisper_onnx_models.py
python whisper_base_en_onnx_infer.py --audio_file jfk.wav --encoder_onnx models-onnx\base.en-encoder.onnx --decoder_onnx models-onnx\base.en-decoder.onnx --mel_filters mel_filters.npz
```

If you want to run the sample code with qnn models.
1. Run the following command.
```bash
   python prepare_whisper_qnn_models.py
```
2. Add the following code at beginning of whisper_base_en_onnx_infer.py.
```python
   from qai_appbuilder import onnxwrapper
```
3. Then run the following command.
```bash
   python whisper_base_en_onnx_infer.py --audio_file jfk.wav --encoder_onnx models-qnn\whisper_base_en-whisperencoder-snapdragon_x_elite.bin --decoder_onnx models-qnn\whisper_base_en-whisperdecoder-snapdragon_x_elite.bin --mel_filters mel_filters.npz
```
## Output
You can see "Transcription: And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country." in log.

