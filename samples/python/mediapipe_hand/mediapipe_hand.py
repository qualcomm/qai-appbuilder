# ---------------------------------------------------------------------
# Copyright (c) 2025 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
from __future__ import annotations
import sys
import os
import time
sys.path.append(".")
sys.path.append("python")
import utils.install as install
import argparse
import cv2
import numpy as np
import torch
from PIL import Image
from utils.image_processing import (
    preprocess_PIL_image,
    resize_pad,
    torch_tensor_to_PIL_image
)

from qai_hub_models.models.mediapipe_hand.model import (
    DETECT_DSCALE,
    DETECT_DXY,
    HAND_LANDMARK_CONNECTIONS,
    MIDDLE_FINDER_KEYPOINT_INDEX,
    ROTATION_VECTOR_OFFSET_RADS,
    WRIST_CENTER_KEYPOINT_INDEX,
)

from qai_hub_models.utils.draw import (
    draw_box_from_corners,
    draw_box_from_xyxy,
    draw_connections,
    draw_points,
)

from qai_hub_models.models._shared.mediapipe.utils import decode_preds_from_anchors
from qai_hub_models.utils.bounding_box_processing import (
    apply_directional_box_offset,
    batched_nms,
    box_xywh_to_xyxy,
    box_xyxy_to_xywh,
    compute_box_affine_crop_resize_matrix,
    compute_box_corners_with_rotation
)

from qai_hub_models.utils.display import display_or_save_image

from qai_hub_models.utils.image_processing import (
    apply_affine_to_coordinates,
    compute_vector_rotation,
    denormalize_coordinates,
    apply_batched_affines_to_frame,
    numpy_image_to_torch
)

from collections.abc import Callable

from PIL.ImageShow import IPythonViewer, _viewers  # type: ignore

from qai_hub_models.models._shared.mediapipe.utils import MediaPipePyTorchAsRoot

import playaudio as audio

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)


####################################################################

MODEL_NAME = "mediapipe_hand"
LANDMARK_DETECTOR_MODEL_ID= "mq3pwv76n"
HAND_DETECTOR_MODEL_ID = "mno1jz9vm"
HAND_DETECTOR_MODEL_NAME = "handdetector"
LANDMARK_DETECTOR_MODEL_NAME = "landmarkdetector"
ESCAPE_KEY_ID = 27
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"

####################################################################

execution_ws = os.getcwd()
qnn_dir = execution_ws + "\\qai_libs"

if not "python" in execution_ws:
    execution_ws = execution_ws + "\\" + "python"

if not MODEL_NAME in execution_ws:
    execution_ws = execution_ws + "\\" + MODEL_NAME

model_dir = execution_ws + "\\models"

hand_detector_model_path = model_dir + "\\" + MODEL_NAME + "-" + HAND_DETECTOR_MODEL_NAME + ".bin"
landmark_detector_model_path = model_dir + "\\" + MODEL_NAME + "-" + LANDMARK_DETECTOR_MODEL_NAME + ".bin"

####################################################################

hand_detector = None
landmark_detector = None
gesture = None
time_save = 0
# demo_mode is False (end_user mode): only draw gesture result. Draw nothing if gesture result is none.
# demo_mode is True (demo mode): draw 21 keypoints, connnections, ROI, palm rectangle and gesture result (if have)
demo_mode = False
audio_loaded = False

# HandDetector class which inherited from the class QNNContext.
class HandDetector(QNNContext):
    def __init__(
        self,
        model_name: str = "None",
        model_path: str = "None",
        detector_anchors: str = "anchors_palm.npy",
    ):
        if model_path is None:
            raise ValueError("model_path must be specified!")
        
        super().__init__(model_name, model_path)

        with MediaPipePyTorchAsRoot():
            from blazepalm import BlazePalm

            handDet = BlazePalm()
            handDet.load_anchors(detector_anchors)
        self.anchors = handDet.anchors

    def Inference(self, input_data):
        input_datas=[input_data]
        output_data1, output_data2 = super().Inference(input_datas)        
        return output_data1, output_data2
    
# LandmarkDetector class which inherited from the class QNNContext.
class LandmarkDetector(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data1, output_data2, output_data3 = super().Inference(input_datas)        
        return output_data1, output_data2, output_data3


def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(LANDMARK_DETECTOR_MODEL_ID, landmark_detector_model_path, desc=desc, fail=fail)
    ret = install.download_qai_hubmodel(HAND_DETECTOR_MODEL_ID, hand_detector_model_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Init():
    global hand_detector, landmark_detector

    model_download()

    time_save = time.time()

    # Config AppBuilder environment.
    QNNConfig.Config(qnn_dir, Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for HandDetector objects.
    hand_detector = HandDetector("hand_detector", hand_detector_model_path)

    # Instance for LandmarkDetector objects.
    landmark_detector = LandmarkDetector("landmark_detector", landmark_detector_model_path)

def convert_image_inputs_to_tensor(
    pixel_values_or_image: np.ndarray | Image | list[Image],
) -> tuple[list[np.ndarray], torch.Tensor]:
    """
    Convert the provided PIL images to a pyTorch tensor with range [0, 1] and shape NCHW - NCHW_fp32_torch_frames
    ~~This does not change channel order. RGB stays RGB, BGR stays BGR, etc~~
    """
    NHWC_int_numpy_frames: list[np.ndarray] = []
    NCHW_fp32_torch_frames: torch.Tensor

    if isinstance(pixel_values_or_image, Image.Image):
        pixel_values_or_image = [pixel_values_or_image]
    if isinstance(pixel_values_or_image, list):
        fp32_frames = []
        for image in pixel_values_or_image:
            NHWC_int_numpy_frames.append(np.array(image.convert("RGB")))
            fp32_frames.append(preprocess_PIL_image(image))
        NCHW_fp32_torch_frames = torch.cat(fp32_frames)
    else:
        assert isinstance(pixel_values_or_image, np.ndarray)
        NHWC_int_numpy_frames = (
            [pixel_values_or_image]
            if len(pixel_values_or_image.shape) == 3
            else [x for x in pixel_values_or_image]
        )
        NCHW_fp32_torch_frames = numpy_image_to_torch(pixel_values_or_image)

    return NHWC_int_numpy_frames, NCHW_fp32_torch_frames

def run_box_detector(
    NCHW_fp32_torch_frames: torch.Tensor
) -> tuple[list[torch.Tensor | None], list[torch.Tensor | None]]:
    """
    From the provided image or tensor, predict the bounding boxes and keypoints of objects detected within.

    Parameters:
        NCHW_fp32_torch_frames: torch.Tensor
            pyTorch tensor (N C H W x fp32, value range is [0, 1]), BGR channel layout

    Returns:
        batched_selected_boxes: list[torch.Tensor | None]
            Selected object bounding box coordinates. None if batch had no bounding boxes with a score above the threshold.
            Shape of each list element is [num_selected_boxes, 2, 2].
                Layout is
                    [[box_x1, box_y1],
                        [box_x2, box_y2]]

        batched_selected_keypoints: list[torch.Tensor | None]
            Selected object bounding box keypoints. None if batch had no bounding boxes with a score above the threshold.
            Shape of each list element is [num_selected_boxes, # of keypoints, 2].
                Layout is
                    [[keypoint_0_x, keypoint_0_y],
                        ...,
                        [keypoint_max_x, keypoint_max_y]]
    """

    # Resize input frames to width 256 and height 256 such that they're the appropriate size for detector inference.
    box_detector_net_inputs, pd_net_input_scale, pd_net_input_pad = resize_pad(NCHW_fp32_torch_frames, (256, 256))

    ############################################################################################################
    # Inference (QNNContext)
    # Input: list[ndarray] with shape [1,256,256,3]
    # Output: 
    # 1. box_scores_ndarray (ndarray) with 2944
    # 2. box_coords_ndarray (ndarray) with 2944*18=52992
    pil_out_image = torch_tensor_to_PIL_image(box_detector_net_inputs[0])
    pil_out_image = np.array(pil_out_image)
    pil_out_image = np.clip(pil_out_image, 0, 255) / 255.0  # normalization
    box_scores_ndarray, box_coords_ndarray = hand_detector.Inference([pil_out_image])
    box_scores = torch.from_numpy(box_scores_ndarray)  # convert to tensor
    box_scores = box_scores.unsqueeze(0)  # change shape to [1,2944]
    box_scores = box_scores.clamp(-100, 100)
    box_scores = box_scores.sigmoid()

    box_coords_ndarray = box_coords_ndarray.reshape(2944, 18)  #reshape from 52992 to (2944, 18)
    box_coords = torch.from_numpy(box_coords_ndarray)  # convert to tensor
    box_coords = box_coords.unsqueeze(0)  # change shape to [1,2944,18]

    # Reshape outputs so that they have shape [..., # of coordinates, 2], where 2 == (x, y). Here, shape is [1,2944,9,2]
    box_coords = box_coords.view(list(box_coords.shape)[:-1] + [-1, 2])
    anchors = hand_detector.anchors.view(
        list(hand_detector.anchors.shape)[:-1] + [-1, 2]
    )

    # Decode to output coordinates using the model's trained anchors.
    decode_preds_from_anchors(box_coords, (256, 256), anchors)

    # Convert box coordinates from CWH -> XYXY format for NMS.
    box_coords[:2] = box_xywh_to_xyxy(box_coords[:2])

    # flatten coords (remove final [2] dim) for NMS
    flattened_box_coords = box_coords.view(list(box_coords.shape)[:-2] + [-1])

    # Run non maximum suppression on the output
    # batched_selected_coords = list[torch.Tensor(shape=[Num Boxes, 4])],
    # where 4 = (x0, y0, x1, y1)
    batched_selected_coords, _ = batched_nms(
        0.3,
        0.95,
        flattened_box_coords,
        box_scores,
    )

    selected_boxes = []
    selected_keypoints = []
    for i in range(0, len(batched_selected_coords)):
        selected_coords = batched_selected_coords[i]
        if len(selected_coords) != 0:
            # Reshape outputs again so that they have shape [..., # of boxes, 2], where 2 == (x, y)
            selected_coords = batched_selected_coords[i].view(
                list(batched_selected_coords[i].shape)[:-1] + [-1, 2]
            )

            denormalize_coordinates(
                selected_coords,
                (256, 256),
                pd_net_input_scale,
                pd_net_input_pad,
            )

            selected_boxes.append(selected_coords[:, :2])
            selected_keypoints.append(selected_coords[:, 2:])
        else:
            selected_boxes.append(None)
            selected_keypoints.append(None)

    return selected_boxes, selected_keypoints

def compute_object_roi(
    batched_selected_boxes: list[torch.Tensor | None],
    batched_selected_keypoints: list[torch.Tensor | None],
) -> list[torch.Tensor | None]:
    """
    From the provided bounding boxes and keypoints, compute the region of interest (ROI) that should be used
    as input to the landmark detection model.

    Parameters:
        batched_selected_boxes: list[torch.Tensor | None]
            Selected object bounding box coordinates. None if batch had no bounding boxes with a score above the threshold.
            Shape of each list element is [num_selected_boxes, 2, 2].
                Layout is
                    [[box_x1, box_y1],
                        [box_x2, box_y2]]

        batched_selected_keypoints: list[torch.Tensor | None]
            Selected object bounding box keypoints. None if batch had no bounding boxes with a score above the threshold.
            Shape of each list element is [num_selected_boxes, # of keypoints, 2].
                Layout is
                    [[keypoint_0_x, keypoint_0_y],
                        ...,
                        [keypoint_max_x, keypoint_max_y]]

    Returns
        batched_roi_4corners: list[torch.Tensor | None]
            Selected object "region of interest" (region used as input to the landmark detector) corner coordinates.
            None if batch had no bounding boxes with a score above the threshold.
            Shape of each list element is [num_selected_boxes, 4, 2], where 2 == (x, y)
            The order of points is  (top left point, bottom left point, top right point, bottom right point)
    """
    batched_selected_roi = []
    for boxes, keypoints in zip(batched_selected_boxes, batched_selected_keypoints):
        if boxes is None or keypoints is None:
            batched_selected_roi.append(None)
            continue

        # Compute bounding box center and rotation
        theta = compute_vector_rotation(
            keypoints[:, WRIST_CENTER_KEYPOINT_INDEX, ...],
            keypoints[:, MIDDLE_FINDER_KEYPOINT_INDEX, ...],
            ROTATION_VECTOR_OFFSET_RADS,
        )
        selected_boxes_cwh = box_xyxy_to_xywh(boxes)
        xc = selected_boxes_cwh[..., 0, 0]
        yc = selected_boxes_cwh[..., 0, 1]
        w = selected_boxes_cwh[..., 1, 0]
        h = selected_boxes_cwh[..., 1, 1]

        # The bounding box often misses the entire object.
        # Move the bounding box slightly (if necessary) to center it with the object.
        apply_directional_box_offset(
            DETECT_DXY * w,
            keypoints[..., WRIST_CENTER_KEYPOINT_INDEX, :],
            keypoints[..., MIDDLE_FINDER_KEYPOINT_INDEX, :],
            xc,
            yc,
        )

        # Apply scaling to enlargen the bounding box
        w *= DETECT_DSCALE
        h *= DETECT_DSCALE

        # Compute box corners from box center, width, height
        batched_selected_roi.append(
            compute_box_corners_with_rotation(xc, yc, w, h, theta)
        )

    return batched_selected_roi

def run_landmark_detector(
    NHWC_int_numpy_frames: list[np.ndarray],
    batched_roi_4corners: list[torch.Tensor | None],
) -> tuple[list[torch.Tensor | None]]:
    """
    From the provided image or tensor, predict the bounding boxes & classes of objects detected within.

    Parameters:
        NHWC_int_numpy_frames:
            List of numpy arrays of shape (H W C x uint8) -- BGR channel layout
            Length of list is # of batches (the number of input images)

            batched_roi_4corners: list[torch.Tensor | None]
                Selected object "region of interest" (region used as input to the landmark detector) corner coordinates.
                None if batch had no bounding boxes with a score above the threshold.
                Shape of each list element is [num_selected_boxes, 4, 2], where 2 == (x, y)
                The order of points is (top left point, bottom left point, top right point, bottom right point)

    Returns:
            batched_selected_landmarks: list[torch.tensor | None]
                Selected landmarks. Organized like the following:
                [
                    # Batch 0 (for Input Image 0)
                    torch.Tensor([
                        Selected Landmark 1 w/ shape (# of landmark points, 3)
                        Selected Landmark 2 w/ shape (# of landmark points, 3)
                        ...
                    ]),
                    # Batch 1 (for Input Image 1)
                    None # (this image has no detected object)
                    ...
                ]
                The shape of each inner list element is [# of landmark points, 3],
                where 3 == (X, Y, Conf)

            ... (additional outputs when needed by implementation)
    """

    # selected landmarks for the ROI (if any)
    # list[torch.Tensor(shape=[Num Selected Landmarks, K, 3])],
    # where K == number of landmark keypoints, 3 == (x, y, confidence)
    #
    # A list element will be None if there is no ROI.
    batched_selected_landmarks: list[torch.Tensor | None] = []

    # For each input image...
    for batch_idx, roi_4corners in enumerate(batched_roi_4corners):
        if roi_4corners is None:
            continue
        affines = compute_box_affine_crop_resize_matrix(
            roi_4corners[:, :3], (256,256)
        )

        # Create input images by applying the affine transforms.
        keypoint_net_inputs = numpy_image_to_torch(
            apply_batched_affines_to_frame(
                NHWC_int_numpy_frames[batch_idx], affines, (256,256)
            )
        )

        ############################################################################################################        
        # Inference (QNNContext)
        # Input: list[ndarray] with shape [1,3,256,256]
        # Output: 
        # 1. ld_scores_ndarray (ndarray) with 1
        # 2. lr_ndarray (ndarray) with 1
        # 3. landmarks_ndarray (ndarray) with 21 * 3 = 63
        keypoint_net_inputs_ndarray = keypoint_net_inputs.numpy()  # convert tensor to ndarray
        ld_scores_ndarray, lr_ndarray, landmarks_ndarray = landmark_detector.Inference(keypoint_net_inputs_ndarray)

        landmarks_ndarray = landmarks_ndarray.reshape(21, 3)  #reshape from 63 to (21, 3)
        landmarks = torch.from_numpy(landmarks_ndarray)  # convert to tensor
        landmarks = landmarks.unsqueeze(0)  # change shape to [1,21,3]
        ld_scores = torch.from_numpy(ld_scores_ndarray)  # convert to tensor

        # Convert [0-1] ranged values of landmarks to integer pixel space.
        landmarks[:, :, 0] *= 256
        landmarks[:, :, 1] *= 256

        # 1 landmark is predicted for each ROI of each input image.
        # For each region of interest & associated predicted landmarks...
        all_landmarks = []
        for ld_batch_idx in range(landmarks.shape[0]):
            # Exclude landmarks that don't meet the appropriate score threshold.
            if ld_scores[ld_batch_idx] >= 0.95:
                # Apply the inverse of affine transform used above to the landmark coordinates.
                # This will convert the coordinates to their locations in the original input image.
                inverted_affine = torch.from_numpy(
                    cv2.invertAffineTransform(affines[ld_batch_idx])
                ).float()
                landmarks[ld_batch_idx][:, :2] = apply_affine_to_coordinates(
                    landmarks[ld_batch_idx][:, :2], inverted_affine
                )

                # Add the predicted landmarks to our list.
                all_landmarks.append(landmarks[ld_batch_idx])

        # Add this batch of landmarks to the output list.
        batched_selected_landmarks.append(
            torch.stack(all_landmarks, dim=0) if all_landmarks else None
        )
    else:
        # Add None for these lists, since this batch has no predicted bounding boxes.
        batched_selected_landmarks.append(None)

    return (batched_selected_landmarks,)

def draw_box_and_roi(
    NHWC_int_numpy_frame: np.ndarray,
    selected_boxes: torch.Tensor,
    selected_keypoints: torch.Tensor,
    roi_4corners: torch.Tensor,
):
    """
    Draw bounding box, keypoints, and corresponding region of interest (ROI) on the provided frame

    Parameters:
        NHWC_int_numpy_frame:
            Numpy array of shape (H W C x uint8) -- BGR channel layout

        selected_boxes: torch.Tensor
            Selected object bounding box coordinates. Shape is [num_selected_boxes, 2, 2].
                Layout is
                    [[box_x1, box_y1],
                    [box_x2, box_y2]]

        selected_keypoints: list[torch.Tensor | None]
            Selected object bounding box keypoints. Shape is [num_selected_boxes, # of keypoints, 2].
                Layout is
                    [[keypoint_0_x, keypoint_0_y],
                    ...,
                    [keypoint_max_x, keypoint_max_y]]

        roi_4corners: list[torch.Tensor | None]
            Selected object "region of interest" (region used as input to the landmark detector) corner coordinates.
            Shape is [num_selected_boxes, 4, 2], where 2 == (x, y)

    Returns
        Nothing; drawing is done on input frame.
    """
    for roi, box, kp in zip(roi_4corners, selected_boxes, selected_keypoints):
        # Draw detector bounding box
        draw_box_from_xyxy(NHWC_int_numpy_frame, box[0], box[1], (255, 0, 0), 1)
        # Draw detector keypoints
        draw_points(NHWC_int_numpy_frame, kp, size=30)
        # Draw region of interest box computed from the detector box & keypoints
        # (this is the input to the landmark detector)
        draw_box_from_corners(NHWC_int_numpy_frame, roi, (0, 255, 0))

def draw_landmarks(
    NHWC_int_numpy_frame: np.ndarray,
    selected_landmarks: torch.Tensor,
    **kwargs,
):
    """
    Draw landmarks on the provided frame

    Parameters:
        NHWC_int_numpy_frame:
            Numpy array of shape (H W C x uint8) -- BGR channel layout

        selected_landmarks
            Selected landmarks. Organized like the following:
                torch.Tensor([
                    Selected Landmark 1 w/ shape (# of landmark points, 3)
                    Selected Landmark 2 w/ shape (# of landmark points, 3)
                    ...
                ]),
                The shape of each inner list element is [# of landmark points, 3],
                where 3 == (X, Y, Conf)

    Returns
        Nothing; drawing is done on input frame.
    """
    for ldm in selected_landmarks:
        # Draw landmark points
        draw_points(NHWC_int_numpy_frame, ldm[:, :2], (0, 255, 0))
        # Draw connections between landmark points
        if HAND_LANDMARK_CONNECTIONS:
            draw_connections(
                NHWC_int_numpy_frame,
                ldm[:, :2],
                HAND_LANDMARK_CONNECTIONS,
                (0, 0, 255),
                2,
            )

def draw_predictions(
    NHWC_int_numpy_frames: list[np.ndarray],
    batched_selected_boxes: list[torch.Tensor | None],
    batched_selected_keypoints: list[torch.Tensor | None],
    batched_roi_4corners: list[torch.Tensor | None],
    batched_selected_landmarks: list[torch.Tensor | None],
    gesture_txt: str,
    demomode: bool,
    **kwargs,
):
    """
    Draw predictions on the provided frame

    Parameters:
        NHWC_int_numpy_frames:
            List of numpy arrays of shape (H W C x uint8) -- BGR channel layout
            Length of list is # of batches (the number of input images)

        batched_selected_boxes: list[torch.Tensor | None]
            Selected object bounding box coordinates. None if batch had no bounding boxes with a score above the threshold.
            Shape of each list element is [num_selected_boxes, 2, 2].
                Layout is
                    [[box_x1, box_y1],
                        [box_x2, box_y2]]

        batched_selected_keypoints: list[torch.Tensor | None]
            Selected object bounding box keypoints. None if batch had no bounding boxes with a score above the threshold.
            Shape of each list element is [num_selected_boxes, # of keypoints, 2].
                Layout is
                    [[keypoint_0_x, keypoint_0_y],
                        ...,
                        [keypoint_max_x, keypoint_max_y]]

        batched_roi_4corners: list[torch.Tensor | None]
            Selected object "region of interest" (region used as input to the landmark detector) corner coordinates.
            None if batch had no bounding boxes with a score above the threshold.
            Shape of each list element is [num_selected_boxes, 4, 2], where 2 == (x, y)
            The order of points is  (top left point, bottom left point, top right point, bottom right point)

        batched_selected_landmarks: list[torch.tensor | None]
            Selected landmarks. Organized like the following:
            [
                # Batch 0 (for Input Image 0)
                torch.Tensor([
                    Selected Landmark 1 w/ shape (# of landmark points, 3)
                    Selected Landmark 2 w/ shape (# of landmark points, 3)
                    ...
                ]),
                # Batch 1 (for Input Image 1)
                None # (this image has no detected object)
                ...
            ]
            The shape of each inner list element is [# of landmark points, 3],
            where 3 == (X, Y, Conf)

    Returns
        Nothing; drawing is done on input frame
    """
    global gesture, time_save
    time_now = time.time()
    if time_now - time_save > 0.9:
        gesture = gesture_txt
        time_save = time_now

    for batch_idx in range(len(NHWC_int_numpy_frames)):
        image = NHWC_int_numpy_frames[batch_idx]
        ld = batched_selected_landmarks[batch_idx]
        box = batched_selected_boxes[batch_idx]
        kp = batched_selected_keypoints[batch_idx]
        roi_4corners = batched_roi_4corners[batch_idx]

        if demomode:
            if box is not None and kp is not None and roi_4corners is not None:
                draw_box_and_roi(image, box, kp, roi_4corners)
            if ld is not None:
                draw_landmarks(image, ld)

        if gesture is not None:
            cv2.putText(image, gesture, (30, 30), cv2.FONT_HERSHEY_DUPLEX, 1, (255, 0, 0), 2)
        else:
            if demomode:
                cv2.putText(image, "Unknown", (30, 30), cv2.FONT_HERSHEY_DUPLEX, 1, (255, 0, 0), 2)

def recognize_gesture(
    landmarks: list[torch.Tensor | None]
) -> str:
    if landmarks[0] is None:
        return None

    # input: list[torch.Tensor] with torch shape [1,21,3]
    # convert shape to [21,3] and convert from tensor to ndarray
    # where, 3 means hand keypoint coordinate [X,Y,confidence]. The origin of coordinate is top-left point. 
    # https://github.com/metalwhale/hand_tracking/blob/b2a650d61b4ab917a2367a05b85765b81c0564f2/run.py
    #        8   12  16  20
    #        |   |   |   |
    #        7   11  15  19
    #    4   |   |   |   |
    #    |   6   10  14  18
    #    3   |   |   |   |
    #    |   5---9---13--17
    #    2    \         /
    #     \    \       /
    #      1    \     /
    #       \    \   /
    #        ------0-
    landmarks_ndarray = landmarks[0].squeeze().numpy()
    thumb_tip   = landmarks_ndarray[4]   # thumb tip coordinate
    thumb_mid   = landmarks_ndarray[3]   # thumb mid coordinate
    thumb_base  = landmarks_ndarray[2]   # thumb base coordinate
    index_tip   = landmarks_ndarray[8]   # index finger tip coordinate
    index_tip2  = landmarks_ndarray[7]   # index finger tip2 coordinate
    index_base  = landmarks_ndarray[5]   # index finger base coordinate
    middle_tip  = landmarks_ndarray[12]  # middle finger tip coordinate
    middle_tip2 = landmarks_ndarray[11]  # middle finger tip2 coordinate
    middle_base = landmarks_ndarray[9]   # middle finger base coordinate
    ring_tip    = landmarks_ndarray[16]  # ring finger tip coordinate
    ring_tip2   = landmarks_ndarray[15]  # ring finger tip2 coordinate
    ring_base   = landmarks_ndarray[13]  # ring finger base coordinate
    little_tip  = landmarks_ndarray[20]  # little finger tip coordinate
    little_tip2 = landmarks_ndarray[19]  # little finger tip2 coordinate
    little_base = landmarks_ndarray[17]  # little finger base coordinate
    wrist       = landmarks_ndarray[0]   # wrist coordinate

    if wrist[1] > thumb_base[1]:   # hand up
        if index_tip[1] > index_base[1] and middle_tip[1] > middle_base[1] and ring_tip[1] > ring_base[1] and little_tip[1] > little_base[1]:   # hand close
            # print(f"thumb: {thumb_tip[0]}, little: {little_tip[0]}, index: {index_tip[0]}")
            if (
                thumb_tip[0] > little_tip[0] and thumb_tip[0] < index_tip[0] + 20
            ) or (
                thumb_tip[0] < little_tip[0] and thumb_tip[0] > index_tip[0] - 20
            ):   # thumb is between little finger and index finger  --> fist
                return "Stop"
            elif thumb_mid[0] - thumb_tip[0] > 5 and thumb_tip[0] < index_tip[0] - 40:     # thumb left --> back 10 seconds
                return "back 10 seconds"
            elif thumb_tip[0] - thumb_mid[0] > 5 and thumb_tip[0] > index_tip[0] + 40:   # thumb right --> fast forward 10 seconds
                return "fast forward 10 seconds"
            else:
                    return None
        elif middle_tip[1] < middle_tip2[1] and ring_tip[1] < ring_tip2[1] and little_tip[1] < little_tip2[1]:   #middle, ring, little open
            # print(f"index_tip: {index_tip[0]}, {index_tip[1]}. thumb: {thumb_tip[0]}, {thumb_tip[1]}")
            if index_tip[1] < index_tip2[1]:   # index finger open
                if (
                    wrist[0] > thumb_base[0] and thumb_tip[0] < thumb_mid[0]   # left hand, thumb open  --> hand open -> pause
                ) or (
                    wrist[0] < thumb_base[0] and thumb_tip[0] > thumb_mid[0]   # right hand, thumb open  --> hand open -> pause
                ):
                    return "Pause"
                else:
                    return None
            elif ((index_tip[0]-thumb_tip[0])**2 + (index_tip[1]-thumb_tip[1])**2)**0.5 < 30:
                return "Play"
            else:
                return None
        else:
            return None
    else:                          # hand down
        return None
    
def predict_landmarks_from_image(
    pixel_values_or_image: np.ndarray | Image | list[Image],
) -> list[np.ndarray]:
    # Read and preprocess the image.    
    NHWC_int_numpy_frames, NCHW_fp32_torch_frames = convert_image_inputs_to_tensor(pixel_values_or_image)

    # Run Bounding Box & Keypoint Detector
    batched_selected_boxes, batched_selected_keypoints = run_box_detector(NCHW_fp32_torch_frames)

    # The region of interest ( bounding box of 4 (x, y) corners).
    # list[torch.Tensor(shape=[Num Boxes, 4, 2])],
    # where 2 == (x, y)
    #
    # A list element will be None if there is no selected ROI.
    batched_roi_4corners = compute_object_roi(batched_selected_boxes, batched_selected_keypoints)

    # selected landmarks for the ROI (if any)
    # list[torch.Tensor(shape=[Num Selected Landmarks, K, 3])],
    # where K == number of landmark keypoints, 3 == (x, y, confidence)
    #
    # A list element will be None if there is no ROI.
    landmarks_out = run_landmark_detector(NHWC_int_numpy_frames, batched_roi_4corners)

    # recognize simple gesture
    gesture_txt = recognize_gesture(landmarks_out[0])

    # draw prediction
    draw_predictions(
        NHWC_int_numpy_frames,
        batched_selected_boxes,
        batched_selected_keypoints,
        batched_roi_4corners,
        *landmarks_out,
        gesture_txt,
        demo_mode,
    )

    return NHWC_int_numpy_frames

def Release():
    global hand_detector, landmark_detector

    # Release the resources.
    del(hand_detector)
    del(landmark_detector)

    if audio_loaded:
        audio.quit()

def handle_audio(
    gesture_text: str
):
    global audio_loaded

    if gesture_text is None or audio_loaded is not True:
        return 
    elif gesture_text == "Play":
        audio.play()
    elif gesture_text == "Stop":
        audio.stop()
    elif gesture_text == "Pause":
        audio.pause()
    elif gesture_text == "fast forward 10 seconds":
        audio.seek(10)
    elif gesture_text == "back 10 seconds":
        audio.seek(-10)

def capture_and_display_processed_frames(
    frame_processor: Callable[[np.ndarray], np.ndarray],
    window_display_name: str,
    cap_device: int = 0,
) -> None:
    """
    Capture frames from the given input camera device, run them through
    the frame processor, and display the outputs in a window with the given name.

    User should press Esc to exit.

    Inputs:
        frame_processor: Callable[[np.ndarray], np.ndarray]
            Processes frames.
            Input and output are numpy arrays of shape (H W C) with BGR channel layout and dtype uint8 / byte.
        window_display_name: str
            Name of the window used to display frames.
        cap_device: int
            Identifier for the camera to use to capture frames.
    """
    global gesture

    cv2.namedWindow(window_display_name)
    capture = cv2.VideoCapture(cap_device)
    if not capture.isOpened():
        raise ValueError("Unable to open video capture.")

    # image_256x256 = Image.open("C:\\Users\\WM\\Desktop\\QAI\\ai-engine-direct-helper\\samples\\python\\mediapipehand\\input.jpg")
    # test_Frame256x256 = np.array(image_256x256.convert("RGB"))

    frame_count = 0
    has_frame, frame = capture.read()
        
    if demo_mode:   # inference all frames
        while has_frame:
            assert isinstance(frame, np.ndarray)

            frame_count = frame_count + 1
            # mirror frame
            frame = np.ascontiguousarray(frame[:, ::-1, ::-1])

            # process & show frame & handle audio
            processed_frame = frame_processor(frame)
            cv2.imshow(window_display_name, processed_frame[:, :, ::-1])
            handle_audio(gesture)

            has_frame, frame = capture.read()
            # frame = test_Frame256x256
            key = cv2.waitKey(1)
            if key == ESCAPE_KEY_ID:
                break
    else:   # inference 1/30 frames
        while has_frame:
            assert isinstance(frame, np.ndarray)

            frame_count = frame_count + 1
            # mirror frame
            frame = np.ascontiguousarray(frame[:, ::-1, ::-1])

            # process & show frame & handle audio
            if frame_count % 30 == 1:
                processed_frame = frame_processor(frame)
                cv2.imshow(window_display_name, processed_frame[:, :, ::-1])
            else:
                if gesture is not None:
                    cv2.putText(frame, gesture, (30, 30), cv2.FONT_HERSHEY_DUPLEX, 1, (255, 0, 0), 2)
                    handle_audio(gesture)
                cv2.imshow(window_display_name, frame[:, :, ::-1])

            has_frame, frame = capture.read()
            # frame = test_Frame256x256
            key = cv2.waitKey(1)
            if key == ESCAPE_KEY_ID:
                break

    capture.release()

def main():
    global demo_mode, audio_loaded

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--imagefile",
        type=str,
        default=None,
        # default="C:\\Users\\WM\\Desktop\\QAI\\ai-engine-direct-helper\\samples\\python\\mediapipehand\\input.jpg",
        help="image file path",
    )
    parser.add_argument(
        "--audiofile",
        type=str,
        default=None,
        # default="C:\\Users\\WM\\Desktop\\QAI\\ai-engine-direct-helper\\samples\\python\\mediapipehand\\sky.wav",
        help="audio file path",
    )
    parser.add_argument(
        "--displayPredict",
        type=str,
        default="True",
        help="display predict image? True or False",
    )
    parser.add_argument(
        "--DemoMode",
        type=str,
        default="True",
        help="Demo mode? True (demo mode) or False (end_user mode)",
    )
    print("Press ESC key to exit if you are opening camera app.")

    args = parser.parse_args()
    demo_mode = True if args.DemoMode == "True" else False

    Init()

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    if args.imagefile:
        orig_image = Image.open(args.imagefile)
        pred_image = predict_landmarks_from_image(orig_image)
        out_image = Image.fromarray(pred_image[0], "RGB")
        if args.displayPredict == "True":
            display_or_save_image(out_image)
        else:
            display_or_save_image(out_image, execution_ws, "output.jpg")
    else:
        if args.audiofile:
            audio_loaded = audio.load_audio(args.audiofile)
        def frame_processor(frame: np.ndarray) -> np.ndarray:
            return predict_landmarks_from_image(frame)[0]  # type: ignore

        capture_and_display_processed_frames(
            frame_processor, "QAIHM Mediapipe Hand Demo"
        )

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    Release()

if __name__ == '__main__':
    main()