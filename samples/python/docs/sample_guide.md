# Guide

## Introduction 
This guide helps developers use QAI AppBuilder with the QNN SDK to execute models on Windows on Snapdragon (WoS) platforms.

## Setup AppBuilder environment and prepare QNN SDK libraries by referring to the links below: 
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md <br>
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md

Set up a new folder named by the model you tend to deploy:
```
C:\ai-hub\model_name\
```

Copy the QNN libraries from QNN SDK to below path:
```
C:\ai-hub\model_name\qai_libs\libqnnhtpv73.cat
C:\ai-hub\model_name\qai_libs\libQnnHtpV73Skel.so
C:\ai-hub\model_name\qai_libs\QnnHtp.dll
C:\ai-hub\model_name\qai_libs\QnnHtpV73Stub.dll
C:\ai-hub\model_name\qai_libs\QnnSystem.dll
```

## Prepare the QNN model
Download the QNN version of the model you wish to deploy from the Qualcomm® AI Hub:
https://aihub.qualcomm.com/compute/models/  

Choose the `TorchScript -> Qualcomm® AI Engine Direct` option and the `Download model` option to download the QNN version of the model for deployment on Windows on Snapdragon (WoS) platforms.

If these options are unavailable, use the following command to export the QNN version of the model:
```
python -m qai_hub_models.models.model_name.export --device "Snapdragon X Elite CRD" --target-runtime qnn
```

Part of the command output is shown below. You can download the model using the following link: [https://app.aihub.qualcomm.com/jobs/j1p86jxog/](https://app.aihub.qualcomm.com/jobs/j1p86jxog/)

 E.g. real_esrgan_general_x4v3:

```
Optimizing model real_esrgan_general_x4v3 to run on-device
Uploading model: 100%|█████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████| 64.7M/64.7M [00:21<00:00, 3.15MB/s]
Scheduled compile job (j1p86jxog) successfully. To see the status and results:
    https://app.aihub.qualcomm.com/jobs/j1p86jxog/
```

Note: The link is generated differently based on the model you tend to deploy.

After downloaded the model, set up a new folder called `models` and copy it to the following path:
```
C:\ai-hub\model_name\models\model_name.bin
```
## Quantize the QNN model

Use the option `--quantize_full_type <type>` for `submit_compile_job` to quantizes an unquantized model to the specified type. It quantizes both activations and weights using a representative dataset. If not such dataset is provided, a randomly generated one is used. In that case, the generated model can be used as a proxy for the achievable performance only, as the model will not be able to produce accurate results. 

Options:

- `int8`: quantize activations and weights to int8
- `int16`: quantize activations and weights to int16
- `w8a16`: quantize weights to int8 and activations to int16 (recommended over int16)
- `w4a8`: quantize weights to int4 and activations to int8
- `w4a16`: quantize weights to int4 and activations to int16

Requirements:

- This option cannot be used if the target runtime is ONNX.

Examples:

Build a new python file called `export.py` and copy the code of `qai_hub_models.models.model_name.export` to this file.  Add one code `compile_options = " --quantize_full_type int8"` to the `export.py` of the model to be quantized and use the following command to export the quantized model:

```
python .\export.py --target-runtime qnn --device "Snapdragon X Elite CRD"  
```

The part of change of the `export.py` is as follows:

```
# 2. Compile the model to an on-device asset
compile_options = " --quantize_full_type int8" #add this code to quantize the model

model_compile_options = model.get_hub_compile_options(
	target_runtime, compile_options + channel_last_flags, hub_device
)
print(f"Optimizing model {model_name} to run on-device")
submitted_compile_job = hub.submit_compile_job(
	model=source_model,
	input_specs=input_spec,
	device=hub_device,
    name=model_name,
    options=model_compile_options,
)
compile_job = cast(hub.client.CompileJob, submitted_compile_job)
```

## Sample Code for Deploying the Model and Executing Inference

The sample code refers to the following path:
https://github.com/quic/ai-engine-direct-helper/blob/main/docs/user_guide.md

Here we take `lama_dilated` as example:

Sample Code (Python):
```
import os
import numpy as np
import torch
import torchvision.transforms as transforms

from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from torch.nn.functional import interpolate, pad
from torchvision import transforms
from typing import Callable, Dict, List, Tuple

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)

image_size = 512
lamadilated = None
image_buffer = None


def preprocess_PIL_image(image: Image) -> torch.Tensor:
    """Convert a PIL image into a pyTorch tensor with range [0, 1] and shape NCHW."""
    transform = transforms.Compose([transforms.Resize(image_size),      # bgr image
                                    transforms.CenterCrop(image_size),
                                    transforms.PILToTensor()])
    img: torch.Tensor = transform(image)  # type: ignore
    img = img.float().unsqueeze(0) / 255.0  # int 0 - 255 to float 0.0 - 1.0
    return img

def torch_tensor_to_PIL_image(data: torch.Tensor) -> Image:
    """
    Convert a Torch tensor (dtype float32) with range [0, 1] and shape CHW into PIL image CHW
    """
    out = torch.clip(data, min=0.0, max=1.0)
    np_out = (out.detach().numpy() * 255).astype(np.uint8)
    return ImageFromArray(np_out)
   
def preprocess_inputs(
    pixel_values_or_image: Image,
    mask_pixel_values_or_image: Image,
) -> Dict[str, torch.Tensor]:

    NCHW_fp32_torch_frames = preprocess_PIL_image(pixel_values_or_image)
    NCHW_fp32_torch_masks = preprocess_PIL_image(mask_pixel_values_or_image)
    
    # The number of input images should equal the number of input masks.
    if NCHW_fp32_torch_masks.shape[0] != 1:
        NCHW_fp32_torch_masks = NCHW_fp32_torch_masks.tile(
            (NCHW_fp32_torch_frames.shape[0], 1, 1, 1)
        )
  
    # Mask input image
    image_masked = (
        NCHW_fp32_torch_frames * (1 - NCHW_fp32_torch_masks) + NCHW_fp32_torch_masks
    )
    
    return {"image": image_masked, "mask": NCHW_fp32_torch_masks}

# LamaDilated class which inherited from the class QNNContext.
class LamaDilated(QNNContext):
    def Inference(self, input_data, input_mask):
        input_datas=[input_data, input_mask]
        output_data = super().Inference(input_datas)[0]
        return output_data
        
def Init():
    global lamadilated

    # Config AppBuilder environment.
    QNNConfig.Config(os.getcwd() + "\\qai_libs", Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for LamaDilated objects.
    lamadilated_model = "models\\lama_dilated.bin"
    lamadilated = LamaDilated("lamadilated", lamadilated_model)

def Inference(input_image_path, input_mask_path, output_image_path):
    global image_buffer

    # Read and preprocess the image&mask.
    image = Image.open(input_image_path)
    mask = Image.open(input_mask_path)   
    inputs = preprocess_inputs(image, mask)
    image_masked, mask_torch = inputs["image"], inputs["mask"]         
    image_masked = image_masked.numpy()
    mask_torch = mask_torch.numpy()
     
    image_masked = np.transpose(image_masked, (0, 2, 3, 1)) 
    mask_torch = np.transpose(mask_torch, (0, 2, 3, 1)) 
             
    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_image = lamadilated.Inference(image_masked, mask_torch)
    
    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()
    
    # show&save the result
    output_image = torch.from_numpy(output_image)    
    output_image = output_image.reshape(image_size, image_size, 3)  
    output_image = torch.unsqueeze(output_image, 0)      
    output_image = [torch_tensor_to_PIL_image(img) for img in output_image]
    image_buffer = output_image[0]
    image_buffer.show()  
    image_buffer.save(output_image_path)

def Release():
    global lamadilated

    # Release the resources.
    del(lamadilated)


Init()

Inference("test_input_image.png", "test_input_mask.png", "out.png")

Release()
```

1. Use the `See more metrics` option from the following link: [https://aihub.qualcomm.com/compute/models/lama_dilated](https://aihub.qualcomm.com/compute/models/lama_dilated) to check the input data format. Here, we can see that:

   `Input Specs`

   `image`: float32[1, 512, 512, 3]

   `mask`: float32[1, 512, 512, 1]

   The input data format for `lama_dilated` is `[N, H, W, C]`, which stands for Number, Height, Width, and Channel.

2. Refer to the following link to learn how to preprocess input data and post-process output data:

   https://github.com/quic/ai-hub-models/blob/main/qai_hub_models/models/lama_dilated/demo.py

   Refer to the following code to check the inference demo for `lama_dilated` in AI Hub:

   ```
   def main(is_test: bool = False):
       repaint_demo(LamaDilated, MODEL_ID, IMAGE_ADDRESS, MASK_ADDRESS, is_test)
   ```

   ```
   # Run repaint app end-to-end on a sample image.
   # The demo will display the predicted image in a window.
   def repaint_demo(
       model_type: Type[BaseModel],
       model_id: str,
       default_image: str | CachedWebAsset,
       default_mask: str | CachedWebAsset,
       is_test: bool = False,
       available_target_runtimes: List[TargetRuntime] = list(
           TargetRuntime.__members__.values()
       ),
   ):
       # Demo parameters
       parser = get_model_cli_parser(model_type)
       parser = get_on_device_demo_parser(
           parser, available_target_runtimes=available_target_runtimes, add_output_dir=True
       )
       parser.add_argument(
           "--image",
           type=str,
           default=default_image,
           help="test image file path or URL",
       )
       parser.add_argument(
           "--mask",
           type=str,
           default=default_mask,
           help="test mask file path or URL",
       )
       args = parser.parse_args([] if is_test else None)
       validate_on_device_demo_args(args, model_id)
   
       # Load image & model
       model = demo_model_from_cli_args(model_type, model_id, args)
       image = load_image(args.image)
       mask = load_image(args.mask)
       print("Model Loaded")
   
       # Run app
       app = RepaintMaskApp(model)
       out = app.paint_mask_on_image(image, mask)[0]
   
       if not is_test:
           display_or_save_image(image, args.output_dir, "input_image.png", "input image")
           display_or_save_image(out, args.output_dir, "output_image.png", "output image")
   ```

   The key function for inference in AI Hub is `app.paint_mask_on_image(image, mask)[0]`:

   ```
       def paint_mask_on_image(
           self,
           pixel_values_or_image: torch.Tensor | np.ndarray | Image | List[Image],
           mask_pixel_values_or_image: torch.Tensor | np.ndarray | Image,
       ) -> List[Image]:
           """
           Erases and repaints the source image[s] in the pixel values given by the mask.
   
           Parameters:
               pixel_values_or_image
                   PIL image(s)
                   or
                   numpy array (N H W C x uint8) or (H W C x uint8) -- both RGB channel layout
                   or
                   pyTorch tensor (N C H W x fp32, value range is [0, 1]), RGB channel layout
   
               mask_pixel_values_or_image
                   PIL image(s)
                   or
                   numpy array (N H W C x uint8) or (H W C x uint8) -- both RGB channel layout
                   or
                   pyTorch tensor (N C H W x fp32, value range is [0, 1]), RGB channel layout
   
                   If one mask is provided, it will be used for every input image.
   
           Returns:
               images: List[PIL.Image]
                   A list of predicted images (one list element per batch).
           """
           inputs = self.preprocess_inputs(
               pixel_values_or_image, mask_pixel_values_or_image
           )
   
           out = self.model(inputs["image"], inputs["mask"])
   
           return [torch_tensor_to_PIL_image(img) for img in out]
   ```

3. Use the code `inputs = self.preprocess_inputs(pixel_values_or_image, mask_pixel_values_or_image)` to preprocess input data (PIL Images). The corresponding sample code is as follows:

   ```
   # Read and preprocess the image&mask.
   image = Image.open(input_image_path)
   mask = Image.open(input_mask_path)   
   inputs = preprocess_inputs(image, mask)
   image_masked, mask_torch = inputs["image"], inputs["mask"]         
   image_masked = image_masked.numpy()
   mask_torch = mask_torch.numpy()
        
   image_masked = np.transpose(image_masked, (0, 2, 3, 1)) 
   mask_torch = np.transpose(mask_torch, (0, 2, 3, 1)) 
   ```

   The function `preprocess_inputs(image, mask)` remains the same as in AI Hub:

   ```
   def preprocess_PIL_image(image: Image) -> torch.Tensor:
       """Convert a PIL image into a pyTorch tensor with range [0, 1] and shape NCHW."""
       transform = transforms.Compose([transforms.Resize(image_size),      # bgr image
                                       transforms.CenterCrop(image_size),
                                       transforms.PILToTensor()])
       img: torch.Tensor = transform(image)  # type: ignore
       img = img.float().unsqueeze(0) / 255.0  # int 0 - 255 to float 0.0 - 1.0
       return img
   ```

   ```
   def preprocess_inputs(
       pixel_values_or_image: Image,
       mask_pixel_values_or_image: Image,
   ) -> Dict[str, torch.Tensor]:
   
       NCHW_fp32_torch_frames = preprocess_PIL_image(pixel_values_or_image)
       NCHW_fp32_torch_masks = preprocess_PIL_image(mask_pixel_values_or_image)
       
       # The number of input images should equal the number of input masks.
       if NCHW_fp32_torch_masks.shape[0] != 1:
           NCHW_fp32_torch_masks = NCHW_fp32_torch_masks.tile(
               (NCHW_fp32_torch_frames.shape[0], 1, 1, 1)
           )
     
       # Mask input image
       image_masked = (
           NCHW_fp32_torch_frames * (1 - NCHW_fp32_torch_masks) + NCHW_fp32_torch_masks
       )
       
       return {"image": image_masked, "mask": NCHW_fp32_torch_masks}
   ```

   Since the input data type for the QNN version model is a `numpy array` with the format `[N, H, W, C]`, we need to convert the output of the `preprocess_inputs` function from `torch` to `numpy` and adjust its format to `[N, H, W, C]`:

   ```
   image_masked = image_masked.numpy()
   mask_torch = mask_torch.numpy()
        
   image_masked = np.transpose(image_masked, (0, 2, 3, 1)) 
   mask_torch = np.transpose(mask_torch, (0, 2, 3, 1)) 
   ```

4. The output data type for the QNN version model is also a `numpy array`. Therefore, we need to convert it to `torch` and reshape it to the format `[N, H, W, C]`, as the format of the output data corresponds to that of the input data:

   ```
   def torch_tensor_to_PIL_image(data: torch.Tensor) -> Image:
       """
       Convert a Torch tensor (dtype float32) with range [0, 1] and shape CHW into PIL image CHW
       """
       out = torch.clip(data, min=0.0, max=1.0)
       np_out = (out.detach().numpy() * 255).astype(np.uint8)
       return ImageFromArray(np_out)
   ```

   ```
   output_image = torch.from_numpy(output_image)    
   output_image = output_image.reshape(image_size, image_size, 3)  
   output_image = torch.unsqueeze(output_image, 0)      
   output_image = [torch_tensor_to_PIL_image(img) for img in output_image]
   ```

   The above corresponds to the code below in AI Hub:

   ```
   out = app.paint_mask_on_image(image, mask)[0]
   ```

   ```
   return [torch_tensor_to_PIL_image(img) for img in out]
   ```

   The final format of the output data is `PIL`.


Note:
1. The input and output data patterns are `numpy arrays`. You may need to convert the output to `torch` for post-processing.
2. Use the `See more metrics` option from the following link: [https://aihub.qualcomm.com/compute/models/lama_dilated](https://aihub.qualcomm.com/compute/models/lama_dilated) to check the input data pattern. The input data pattern might be `[N, H, W, C]` or `[N, C, W, H]`, and the inference results will vary significantly if the input data pattern is incorrect. Add the following code to the sample code for pattern conversion:
```
input_data = np.transpose(input_data, (0, 2, 3, 1)) 
```
3. The output of the inference is `numpy array`, you need to transfer it to torch and reshape it correctly. The shape of the output data corresponds to that of input data.

Copy the input data, e.g., a sample 512x512 image and mask, to the following path:
```
C:\ai-hub\lama_dilated\test_input_image.png
C:\ai-hub\lama_dilated\test_input_mask.png
```

Run the sample code:	
```
python lama_dilated.py
```

Special Note:

1. When run the inference of some model e.g.  `fastSam_x` or `yolov8_det` which may use ultralytics library, please do not install the latest version in case of some error when running inference. Use the following command to install the designated version of ultralytics library:

   ```
   pip install ultralytics==8.0.193
   ```

2. When run the inference of some model e.g.  `fastSam_x` or `yolov8_det` which may use the function `torchvision.ops.nms` to postprocess the result of inference, you may meet some error like:
   ```
   Traceback (most recent call last):
     File "C:\taonan\fastsam_x\fastsam_x.py", line 135, in <module>
       Inference("in.jpg", "out.jpg")
     File "C:\taonan\fastsam_x\fastsam_x.py", line 94, in Inference
       p = ops.non_max_suppression(
           ^^^^^^^^^^^^^^^^^^^^^^^^
     File "C:\Programs\Python\Python311-arm64\Lib\site-packages\ultralytics\utils\ops.py", line 291, in non_max_suppression
       i = torchvision.ops.nms(boxes, scores, iou_thres)  # NMS
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
     File "C:\Programs\Python\Python311-arm64\Lib\site-packages\torchvision\ops\boxes.py", line 41, in nms
       return torch.ops.torchvision.nms(boxes, scores, iou_threshold)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
     File "C:\Programs\Python\Python311-arm64\Lib\site-packages\torch\_ops.py", line 854, in __call__
       return self_._op(*args, **(kwargs or {}))
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
   NotImplementedError: Could not run 'torchvision::nms' with arguments from the 'CPU' backend. This could be because the operator doesn't exist for this backend, or was omitted during the selective/custom build process (if using custom build). If you are a Facebook employee using PyTorch on mobile, please visit https://fburl.com/ptmfixes for possible resolutions. 'torchvision::nms' is only available for these backends: [CUDA, Meta, QuantizedCUDA, BackendSelect, Python, FuncTorchDynamicLayerBackMode, Functionalize, Named, Conjugate, Negative, ZeroTensor, ADInplaceOrView, AutogradOther, AutogradCPU, AutogradCUDA, AutogradXLA, AutogradMPS, AutogradXPU, AutogradHPU, AutogradLazy, AutogradMeta, Tracer, AutocastCPU, AutocastXPU, AutocastCUDA, AutocastPrivateUse1, FuncTorchBatched, BatchedNestedTensor, FuncTorchVmapMode, Batched, VmapMode, FuncTorchGradWrapper, PythonTLSSnapshot, FuncTorchDynamicLayerFrontMode, PreDispatch, PythonDispatcher].
   
   ```

   It may be caused by the difference of environment between WoS and x86, so please replace the function `torchvision.ops.nms` with another function made by ourselves, you can name it as anything you like, e.g. `custom_nms`:
   ```
   def custom_nms(boxes, scores, iou_threshold):
       '''
       self definition of nms function cause nms from torch is not avaliable on this device without cuda
       '''
       
       if len(boxes) == 0:
           return torch.empty((0,), dtype=torch.int64)
       
       # transfer to numpy array
       boxes_np = boxes.cpu().numpy()
       scores_np = scores.cpu().numpy()
   
       # get the coor of boxes
       x1 = boxes_np[:, 0]
       y1 = boxes_np[:, 1]
       x2 = boxes_np[:, 2]
       y2 = boxes_np[:, 3]
   
       # compute the area of each single boxes
       areas = (x2 - x1 + 1) * (y2 - y1 + 1)
       order = scores_np.argsort()[::-1]
   
       keep = []
       while order.size > 0:
           i = order[0]
           keep.append(i)
           xx1 = np.maximum(x1[i], x1[order[1:]])
           yy1 = np.maximum(y1[i], y1[order[1:]])
           xx2 = np.minimum(x2[i], x2[order[1:]])
           yy2 = np.minimum(y2[i], y2[order[1:]])
   
           w = np.maximum(0.0, xx2 - xx1 + 1)
           h = np.maximum(0.0, yy2 - yy1 + 1)
           inter = w * h
           ovr = inter / (areas[i] + areas[order[1:]] - inter)
   
           inds = np.where(ovr <= iou_threshold)[0]
           order = order[inds + 1]
   
       return torch.tensor(keep, dtype=torch.int64)
   ```
   
   3. When run the inference of model `fastsam_x`, specifically the `text_prompt` function of this model shown by the following code:
   
      ```
      prompt_process = FastSAMPrompt(image_path[0], results, device="cpu")
      segmented_result = prompt_process.text_prompt(text='the yellow dog')
      ```
   
      please change the function `def text_prompt(self, text):` of the following python file: 
   
      ````
      C:\Programs\Python\Python311-arm64\Lib\site-packages\ultralytics\models\fastsam\prompt.py
      ````
   
      specifically change the code `self.results[0].masks.data = torch.tensor(np.array([ann['segmentation'] for ann in annotations]))` to 
   
      `self.results[0].masks.data = torch.tensor(np.array([annotations[max_idx]['segmentation']]))`.
   
      The detail of the changed function  `def text_prompt(self, text):` is as follows:
   
      ```
      def text_prompt(self, text):
      	if self.results[0].masks is not None:
          	format_results = self._format_results(self.results[0], 0)
              cropped_boxes, cropped_images, not_crop, filter_id, annotations = self._crop_image(format_results)
              clip_model, preprocess = self.clip.load('ViT-B/32', device=self.device)
              scores = self.retrieve(clip_model, preprocess, cropped_boxes, text, device=self.device)
              max_idx = scores.argsort()
              max_idx = max_idx[-1]
              max_idx += sum(np.array(filter_id) <= int(max_idx))
              self.results[0].masks.data = torch.tensor(np.array([annotations[max_idx]['segmentation']]))
          return self.results
      ```

## Output

The output, e.g. output image will be saved to the following path:
```
C:\ai-hub\model_name\out.png
```

Use the following command to compare the results on Windows on Snapdragon (WoS) platforms and the Host Cloud Device:
```
python -m qai_hub_models.models.model_name.demo --on-device --hub-model-id m1m6gor6q --device "Snapdragon X Elite CRD"
```
