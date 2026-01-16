# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
import shutil
sys.path.append(".")
sys.path.append("python")
import utils.install as install
import cv2
import numpy as np
import torch
import torch.nn.functional as F
import time
import argparse

from PIL import ImageFont, ImageDraw, Image
from torch.utils.data import DataLoader

from easyocr.craft_utils import adjustResultCoordinates, getDetBoxes
from easyocr.imgproc import normalizeMeanVariance, resize_aspect_ratio
from easyocr.recognition import AlignCollate, ListDataset, custom_mean
from easyocr.utils import (
    diff,
    get_image_list,
    group_text_box,
    make_rotated_img_list,
    reformat_input,
    set_result_with_confidence,
)

from easyocr.utils import CTCLabelConverter

from qai_hub_models.utils.asset_loaders import load_image
from qai_hub_models.utils.display import display_or_save_image

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig, timer)
from pathlib import Path

DETECTOR_ARGS = {
    "canvas_size": 2560,
    "mag_ratio": 1.0,
    "estimate_num_chars": False,
    "text_threshold": 0.7,
    "link_threshold": 0.4,
    "low_text": 0.4,
    "poly": False,
    "estimate_num_chars": False,
    "optimal_num_chars": None,
    "slope_ths": 0.1,
    "ycenter_ths": 0.5,
    "height_ths": 0.5,
    "width_ths": 0.5,
    "add_margin": 0.1,
    "min_size": 20,
}

RECOGNIZER_ARGS = {
    "allowlist": None,
    "blocklist": None,
    "beamWidth": 5,
    "detail": 1,
    "rotation_info": None,
    "contrast_ths": 0.1,
    "adjust_contrast": 0.5,
    "filter_ths": 0.003,
}

IMAGE_SIZE_W = 800
IMAGE_SIZE_H = 608

from utils.image_processing import (
    pil_resize_pad,
    app_to_net_image_inputs,
    pil_undo_resize_pad
)
####################################################################

MODEL_NAME = "easy_ocr"
Detector_MODEL_ID = "mnov7w2vm"
Recognizer_MODEL_ID = "mn4xr1lwq"
CN_Detector_MODEL_ID = "mmd9j7dwn"    
CN_Recognizer_MODEL_ID = "mnzoyjvdm"
HUB_ID_SY = "e2f9ca99e756302ac3f604f1169da1243acfec45"

Detector_MODEL_LINK = MODEL_NAME + "_EasyOCRDetector.bin"
Recognizer_MODEL_LINK = MODEL_NAME + "_EasyOCRRecognizer.bin"

#Detector_MODEL_SIZE     = 42237472
#Recognizer_MODEL_SIZE   = 12173040

CN_Detector_MODEL_LINK = MODEL_NAME + "_EasyOCRDetector_Ch_En.bin"
CN_Recognizer_MODEL_LINK = MODEL_NAME + "_EasyOCRRecognizer_Ch_En.bin"

#Detector_MODEL_SIZE     = 42237600
#Recognizer_MODEL_SIZE   = 15638384
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
INPUT_IMAGE_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/easyocr/v1/english.png"

####################################################################

execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if not "python" in str(execution_ws):
    execution_ws = execution_ws / "python"

if not MODEL_NAME in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"
sd_dir = model_dir

detector_model_path = sd_dir / Detector_MODEL_LINK
recognizer_model_path = sd_dir / Recognizer_MODEL_LINK

cn_detector_model_path = sd_dir / CN_Detector_MODEL_LINK
cn_recognizer_model_path = sd_dir / CN_Recognizer_MODEL_LINK

# char_file and lang_char_file holds strings for charactor mapping, with different encoding.

#char_file = execution_ws + "\\Char\\" + "en_character.bin"
#lang_char_file = execution_ws + "\\Char\\" + "en_character.bin"

char_file = execution_ws / "Char" / "ch_en_character.bin"
lang_char_file = execution_ws / "Char" / "ch_en_lang_char.bin"
#input_image_path = execution_ws + "\\english.png"
input_image_path = execution_ws / "ch_en.png"

font_path = execution_ws / "simsun.ttc"
SIMSUN_TTC_URL = "https://git.imagedt.com/shixin/pdtttools/-/raw/master/fonts/simsun.ttc"

# default_font_path = "C:\\Windows\\Fonts\\simsun.ttc"
##################################################################

# model objects.
detector_model_obj = None
recognizer_model_obj = None

class EasyOCR_Detector(QNNContext):
    def Inference(self, input_data):
        print("Calling EasyOCR_Detector::Inference on NPU")
        input_datas=[input_data]
        output_data = super().Inference(input_datas)
        return output_data

class EasyOCR_Recognizer(QNNContext):
    def Inference(self, input_data):
        print("Calling EasyOCR_Recognizer::Inference on NPU")
        input_datas=[input_data]
        output_data = super().Inference(input_datas)    
        return output_data


def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"

    ret = install.download_qai_hubmodel(Detector_MODEL_ID, detector_model_path, desc=desc, fail=fail, hub_id=HUB_ID_SY)
    ret = install.download_qai_hubmodel(Recognizer_MODEL_ID, recognizer_model_path, desc=desc, fail=fail, hub_id=HUB_ID_SY)
    ret = install.download_qai_hubmodel(CN_Detector_MODEL_ID, cn_detector_model_path, desc=desc, fail=fail, hub_id=HUB_ID_SY)
    ret = install.download_qai_hubmodel(CN_Recognizer_MODEL_ID, cn_recognizer_model_path, desc=desc, fail=fail, hub_id=HUB_ID_SY) 
    
    if not os.path.exists(font_path):
        ret = install.download_url(SIMSUN_TTC_URL, font_path)

    if not ret:
        exit()


def Init():
    global detector_model_obj, recognizer_model_obj, font_path
    # Load model
    model_download()

    SetQNNConfig()
    
    #detector_model_obj = EasyOCR_Detector("EasyOCRDetector", detector_model_path)
    #recognizer_model_obj = EasyOCR_Recognizer("EasyOCRRecognizer", recognizer_model_path)
    detector_model_obj = EasyOCR_Detector("EasyOCRDetector", str(cn_detector_model_path))
    recognizer_model_obj = EasyOCR_Recognizer("EasyOCRRecognizer", str(cn_recognizer_model_path))
    # ----------------
    
def main(Image_Path: str = None):
    # Load app and image
    if Image_Path is None:
        if not os.path.exists(input_image_path):
            ret = True
            ret = install.download_url(INPUT_IMAGE_PATH_URL, input_image_path)
        Image_Path = input_image_path
    
    orig_image = load_image(Image_Path)
    image, scale, padding = pil_resize_pad(orig_image, (IMAGE_SIZE_H, IMAGE_SIZE_W))

    # Language list
    #lang_list = ["en"]
    lang_list = ["en","ch_sim"]

    NHWC_int_numpy_frames, _ = app_to_net_image_inputs(image, True)

    batch_size = len(NHWC_int_numpy_frames)
    i = 0

    NHWC_int_numpy_frame = NHWC_int_numpy_frames[0]

    img, img_cv_grey = reformat_input(NHWC_int_numpy_frame)

    # detector
    input_tensor, infos = Demo_Local_detector_preprocess(img)

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    feature, results = detector_model_obj.Inference(input_tensor)

    # recognizer
    recognizer_obj = Local_EasyOCR_recognizer(recognizer_model_obj.Inference, lang_list)


    # reshape the detector output for detector PP    
    results_tensor = torch.from_numpy(results)
    results_tensor = results_tensor.reshape(1, 304, 400, 2)

    feature_tensor = torch.from_numpy(feature)
    feature_tensor = feature_tensor.reshape(1, 32, 304, 400)

    horizontal_list, free_list = Demo_Local_detector_postprocess(results_tensor, infos)
    horizontal_list, free_list = horizontal_list[0], free_list[0]
    (
        img_cv_grey,
        horizontal_list,
        free_list,
        ignore_char,
        batch_size,
    ) = recognizer_obj.recognizer_preprocess(
        img_cv_grey, horizontal_list, free_list, batch_size
    )

    list_result = recognizer_obj.recognize(img_cv_grey, horizontal_list, free_list, batch_size, ignore_char)
    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal() 

    results2 = recognizer_obj.recognizer_postprocess(NHWC_int_numpy_frames,NHWC_int_numpy_frame, list_result)

    final_image = pil_undo_resize_pad(results2[0],orig_image.size,scale,padding)
    display_or_save_image(final_image, None)


def SetQNNConfig():
    QNNConfig.Config(str(qnn_dir), Runtime.HTP, LogLevel.ERROR, ProfilingLevel.BASIC)

def Release():
    global detector_model_obj
    global recognizer_model_obj

    # Release the resources.
    del(detector_model_obj)
    del(recognizer_model_obj)

def Demo_Local_detector_preprocess(img: np.ndarray):
    image_arrs = [img]

    img_resized_list = []

    # resize
    for img in image_arrs:
        img_resized, target_ratio, size_heatmap = resize_aspect_ratio(
            img,
            DETECTOR_ARGS["canvas_size"],
            interpolation=cv2.INTER_LINEAR,
            mag_ratio=DETECTOR_ARGS["mag_ratio"],
        )
        img_resized_list.append(img_resized)

    ratio_h = ratio_w = 1 / target_ratio
    # preprocessing
    x = [
        np.transpose(normalizeMeanVariance(n_img), (2, 0, 1))
        for n_img in img_resized_list
    ]
    x = np.array(x)

    return x, (ratio_h, ratio_w)

def Demo_Local_detector_postprocess(results: torch.Tensor, infos: torch.Tensor):
    ratio_w = infos[1]
    ratio_h = infos[0]
    result, horizontal_list_agg, free_list_agg = [], [], []

    boxes_list, polys_list = [], []
    for out in results:
        # make score and link map
        score_text = out[:, :, 0].cpu().data.numpy()
        score_link = out[:, :, 1].cpu().data.numpy()

        # Post-processing
        required_keys = [
            "textmap",
            "linkmap",
            "text_threshold",
            "link_threshold",
            "low_text",
            "poly",
            "estimate_num_chars",
        ]
        filtered_args = {
            k: DETECTOR_ARGS[k] for k in required_keys if k in DETECTOR_ARGS
        }

        boxes, polys, mapper = getDetBoxes(score_text, score_link, **filtered_args)

        # coordinate adjustment
        boxes = adjustResultCoordinates(boxes, ratio_w, ratio_h)
        polys = adjustResultCoordinates(polys, ratio_w, ratio_h)
        if DETECTOR_ARGS["estimate_num_chars"]:
            boxes = list(boxes)
            polys = list(polys)
        for k in range(len(polys)):
            if DETECTOR_ARGS["estimate_num_chars"]:
                boxes[k] = (boxes[k], mapper[k])
            if polys[k] is None:
                polys[k] = boxes[k]
        boxes_list.append(boxes)
        polys_list.append(polys)

    if DETECTOR_ARGS["estimate_num_chars"]:
        polys_list = [
            [
                p
                for p, _ in sorted(
                    polys,
                    key=lambda x: abs(DETECTOR_ARGS["optimal_num_chars"] - x[1]),
                )
            ]
            for polys in polys_list
        ]

    for polys in polys_list:
        single_img_result = []
        for i, box in enumerate(polys):
            poly = np.array(box).astype(np.int32).reshape(-1)
            single_img_result.append(poly)
        result.append(single_img_result)

    required_keys = [
        "slope_ths",
        "ycenter_ths",
        "height_ths",
        "width_ths",
        "add_margin",
    ]
    filtered_args = {
        k: DETECTOR_ARGS[k] for k in required_keys if k in DETECTOR_ARGS
    }
    for text_box in result:
        horizontal_list, free_list = group_text_box(text_box, **filtered_args)
        if DETECTOR_ARGS["min_size"]:
            horizontal_list = [
                i
                for i in horizontal_list
                if max(i[1] - i[0], i[3] - i[2]) > DETECTOR_ARGS["min_size"]
            ]
            free_list = [
                i
                for i in free_list
                if max(diff([c[0] for c in i]), diff([c[1] for c in i]))
                > DETECTOR_ARGS["min_size"]
            ]
        horizontal_list_agg.append(horizontal_list)
        free_list_agg.append(free_list)

    return horizontal_list_agg, free_list_agg

def draw_box_from_xyxy(
    frame: np.ndarray,
    top_left: np.ndarray | torch.Tensor | tuple[int, int],
    bottom_right: np.ndarray | torch.Tensor | tuple[int, int],
    color: tuple[int, int, int] = (0, 0, 0),
    size: int = 3,
    text: [str] = None,
):
    """
    Draw a box using the provided top left / bottom right points to compute the box.

    Parameters:
        frame: np.ndarray
            np array (H W C x uint8, BGR)

        box: np.ndarray | torch.Tensor
            array (4), where layout is
                [xc, yc, h, w]

        color: tuple[int, int, int]
            Color of drawn points and connection lines (RGB)

        size: int
            Size of drawn points and connection lines BGR channel layout

        text: None | str
            Overlay text at the top of the box.

    Returns:
        None; modifies frame in place.
    """
    if not isinstance(top_left, tuple):
        top_left = (int(top_left[0].item()), int(top_left[1].item()))
    if not isinstance(bottom_right, tuple):
        bottom_right = (int(bottom_right[0].item()), int(bottom_right[1].item()))

    cv2.rectangle(frame, top_left, bottom_right, color, size)
    if text is not None:
        cv2.putText(
            frame,
            text,
            (top_left[0], top_left[1] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            size,
        )
        
class Local_EasyOCR_recognizer:
    def __init__(
        self,
        recognizer,
        lang_list,
    ):
        self.imgH = 64
        self.decoder = "greedy"

        self.character = "0123456789!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ €ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" #ocr_reader.character
        self.lang_char = "" #ocr_reader.lang_char
        #self.model_lang = "english"
        self.model_lang = "chinese_sim"
        self.separator_list = {} # dict of separator
        self.dict_list = {}


        self.recognizer = recognizer

        # read self.character from file 
        with open(char_file, "rb") as file:
            char_bin = file.read()
        self.character = char_bin.decode('gb18030')

        # read self.lang_char from file 
        with open(lang_char_file, "rb") as file:
            lang_char_bin = file.read()
        self.lang_char = lang_char_bin.decode('gb18030')
        self.converter = CTCLabelConverter(self.character, self.separator_list, self.dict_list)
        
    def recognizer_preprocess(
        self,
        img_cv_grey: np.ndarray,
        horizontal_list: list[list[int]] | None,
        free_list: list[list[int]] | None,
        batch_size: int,
    ):
        if RECOGNIZER_ARGS["allowlist"]:
            ignore_char = "".join(
                set(self.character) - set(RECOGNIZER_ARGS["allowlist"])  # type: ignore
            )
        elif RECOGNIZER_ARGS["blocklist"]:
            ignore_char = "".join(set(RECOGNIZER_ARGS["blocklist"]))  # type: ignore
        else:
            ignore_char = "".join(set(self.character) - set(self.lang_char))

        if self.model_lang in ["chinese_tra", "chinese_sim"]:
            self.decoder = "greedy"

        if (horizontal_list is None) and (free_list is None):
            y_max, x_max = img_cv_grey.shape
            horizontal_list = [[0, x_max, 0, y_max]]
            free_list = []

        return img_cv_grey, horizontal_list, free_list, ignore_char, batch_size

    def recognize(
        self,
        img_cv_grey: np.ndarray,
        horizontal_list: list[list[int]],
        free_list: list[list[int]],
        batch_size: int,
        ignore_char: str,
    ):
        if batch_size == 1 and not RECOGNIZER_ARGS["rotation_info"]:
            result = []

            for bbox in horizontal_list:
                h_list = [bbox]
                f_list = []  # type: ignore
                image_list, max_width = get_image_list(
                    h_list, f_list, img_cv_grey, model_height=self.imgH
                )
                result0 = self.recognizer_get_text(
                    int(max_width), image_list, ignore_char, batch_size
                )
                result += result0
            for bbox in free_list:
                h_list = []
                f_list = [bbox]
                image_list, max_width = get_image_list(
                    h_list, f_list, img_cv_grey, model_height=self.imgH
                )
                result0 = self.recognizer_get_text(
                    int(max_width), image_list, ignore_char, batch_size
                )
                result += result0

        # default mode will try to process multiple boxes at the same time
        else:
            image_list, max_width = get_image_list(
                horizontal_list, free_list, img_cv_grey, model_height=self.imgH
            )
            image_len = len(image_list)
            if RECOGNIZER_ARGS["rotation_info"] and image_list:
                image_list = make_rotated_img_list(
                    RECOGNIZER_ARGS["rotation_info"], image_list
                )
                max_width = max(max_width, self.imgH)

            result = self.recognizer_get_text(
                int(max_width), image_list, ignore_char, batch_size
            )

            if RECOGNIZER_ARGS["rotation_info"] and (horizontal_list + free_list):
                # Reshape result to be a list of lists, each row being for
                # one of the rotations (first row being no rotation)
                result = set_result_with_confidence(
                    [
                        result[image_len * i : image_len * (i + 1)]
                        for i in range(len(RECOGNIZER_ARGS["rotation_info"]) + 1)  # type: ignore
                    ]
                )
        return result

    def recognizer_get_text(
        self,
        imgW: int,
        image_list: list[np.ndarray],
        ignore_char: str = "",
        batch_size: int = 1,
        workers: int = 1,
    ):
        ignore_idx = []
        for char in ignore_char:
            ignore_idx.append(self.character.index(char) + 1)

        coord: list[np.ndarray] = [item[0] for item in image_list]
        img_list = [item[1] for item in image_list]
        AlignCollate_normal = AlignCollate(
            imgH=self.imgH, imgW=imgW, keep_ratio_with_pad=True
        )
        test_data = ListDataset(img_list)
        test_loader = DataLoader(
            test_data,
            batch_size=batch_size,
            shuffle=False,
            num_workers=int(workers),
            collate_fn=AlignCollate_normal,
            pin_memory=False,
        )

        # predict first round
        result1 = self.recognizer_inference(test_loader, ignore_idx)

        # predict second round
        low_confident_idx = [
            i
            for i, item in enumerate(result1)
            if (item[1] < RECOGNIZER_ARGS["contrast_ths"])  # type: ignore
        ]

        result2 = []
        if len(low_confident_idx) > 0:
            img_list2 = [img_list[i] for i in low_confident_idx]
            AlignCollate_contrast = AlignCollate(
                imgH=self.imgH,
                imgW=imgW,
                keep_ratio_with_pad=True,
                adjust_contrast=RECOGNIZER_ARGS["adjust_contrast"],
            )
            test_data = ListDataset(img_list2)
            test_loader = DataLoader(
                test_data,
                batch_size=batch_size,
                shuffle=False,
                num_workers=int(workers),
                collate_fn=AlignCollate_contrast,
                pin_memory=False,
            )
            result2 = self.recognizer_inference(test_loader, ignore_idx)

        result = []
        for i, zipped in enumerate(zip(coord, result1)):
            box, pred1 = zipped
            if i in low_confident_idx:
                pred2 = result2[low_confident_idx.index(i)]
                if pred1[1] > pred2[1]:
                    result.append((box, pred1[0], pred1[1]))
                else:
                    result.append((box, pred2[0], pred2[1]))
            else:
                result.append((box, pred1[0], pred1[1]))

        return result

    def recognizer_inference(
        self, test_loader: DataLoader, ignore_idx: list[int]
    ):
        result = []

        with torch.no_grad():
            for image_tensors in test_loader:
                batch_size = image_tensors.size(0)

                image = image_tensors

                # Till now, image is [1, 1, 64, ?] but the model need [1, 1, 64, 1000]
                new_image = torch.cat((image, image[:, :, :, 0:1]), dim=3)
                
                for i in range(image.shape[3], 999):
                    new_image = torch.cat((new_image, new_image[:, :, :, 0:1]), dim=3)
                
                preds = self.recognizer(new_image.detach().cpu().numpy())

                # preds is a list but we need [1, X, 97] for En
                #                             [1, X, 6719] for Ch
                #reshaped_preds = preds[0].reshape(1, -1, 97)
                reshaped_preds = preds[0].reshape(1, -1, 6719)
                reshaped_preds = torch.from_numpy(reshaped_preds)
                # Select max probabilty (greedy decoding) then decode index to character
                preds_size = torch.IntTensor([reshaped_preds.size(1)] * batch_size)

                ######## filter ignore_char, rebalance
                preds_prob = F.softmax(reshaped_preds, dim=2)
                preds_prob[:, :, ignore_idx] = 0.0
                pred_norm = preds_prob.sum(dim=2)
                preds_prob = preds_prob / pred_norm.unsqueeze(-1)

                preds_str: list[str]
                if self.decoder == "greedy":
                    # Select max probabilty (greedy decoding) then decode index to character
                    _, preds_index = preds_prob.max(2)
                    preds_index = preds_index.view(-1)
                    preds_str = self.converter.decode_greedy(
                        preds_index.data.cpu().detach().numpy(), preds_size.data
                    )
                elif self.decoder == "beamsearch":
                    k = preds_prob.cpu().detach().numpy()
                    preds_str = self.converter.decode_beamsearch(
                        k, beamWidth=RECOGNIZER_ARGS["beamWidth"]
                    )
                elif self.decoder == "wordbeamsearch":
                    k = preds_prob.cpu().detach().numpy()
                    preds_str = self.converter.decode_wordbeamsearch(
                        k, beamWidth=RECOGNIZER_ARGS["beamWidth"]
                    )
                else:
                    raise NotImplementedError(f"Unknown decoder {self.decoder}")

                preds_prob_np = preds_prob.cpu().detach().numpy()
                values = preds_prob_np.max(axis=2)
                indices = preds_prob_np.argmax(axis=2)
                preds_max_prob = []
                for v, i in zip(values, indices):
                    max_probs = v[i != 0]
                    if len(max_probs) > 0:
                        preds_max_prob.append(max_probs)
                    else:
                        preds_max_prob.append(np.array([0]))

                for pred, pred_max_prob in zip(preds_str, preds_max_prob):
                    confidence_score = custom_mean(pred_max_prob)
                    result.append([pred, confidence_score])

        return result

    def recognizer_postprocess(
        self,
        input_NHWC_int_numpy_frames,
        input_NHWC_int_numpy_frame,
        list_result
    ):
        i=0
        b,g,r,a = 0,0,255,0

        coords = [item[0] for item in list_result]
        texts = [item[1] for item in list_result]
        confs = [item[2] for item in list_result]
        # TODO: Use PIL to draw Chinese
        for coord in coords:
            coord[0] = [int(x) for x in coord[0]]
            coord[2] = [int(x) for x in coord[2]]
            draw_box_from_xyxy(
                input_NHWC_int_numpy_frame,
                tuple(coord[0]),
                tuple(coord[2]),
                color=(0, 255, 0),
                size=2,
                #text=texts[i],
                )
            print(texts[i])

            i=i+1

        i=0
        font_Ch = ImageFont.truetype(font_path, 18)
        img_pil = Image.fromarray(input_NHWC_int_numpy_frames[0])
        for coord in coords:
            ## Use simsum.ttc to write Chinese.            
            draw = ImageDraw.Draw(img_pil)
            coord_x = coord[0][0]
            coord_y = coord[0][1]
            draw.text((coord_x,coord_y-20),  text = texts[i], font = font_Ch, fill = (b, g, r, a))
            i=i+1
        input_NHWC_int_numpy_frames[0] = np.array(img_pil)

        return (Image.fromarray(input_NHWC_int_numpy_frames[0]), list_result)
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--Image_Path", default=None, type=str)
    args = parser.parse_args()
    
    Init()
    main(args.Image_Path)
    Release()
