from ultralytics import YOLO

# Load the YOLO26 model
model = YOLO("yolo26n.pt or your .pt model path")

# Export the model to ONNX format
model.export(format="onnx")  # creates 'yolo26n.onnx'

# Load the exported ONNX model
onnx_model = YOLO("your .onnx model path")

# Run inference
results = onnx_model("your input image")