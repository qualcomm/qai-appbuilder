# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
import sys
import os
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "common"))
import install
import argparse
import torch
import os
import numpy as np
from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Video utilities (from qai_hub_models.datasets.kinetics400.video_utils)
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_video(video: torch.Tensor) -> torch.Tensor:
    """Normalize video frames: [T,H,W,C] uint8 → [C,T,H,W] float32 in [0,1]."""
    return video.permute(3, 0, 1, 2).to(torch.float32) / 255


def _resize_video(video: torch.Tensor, size: tuple) -> torch.Tensor:
    """Bilinear resize of a [C,T,H,W] video tensor."""
    return torch.nn.functional.interpolate(
        video, size=size, scale_factor=None, mode="bilinear", align_corners=False
    )


def _crop_video(video: torch.Tensor, output_size: tuple) -> torch.Tensor:
    """Center-crop a [C,T,H,W] video tensor."""
    h, w = video.shape[-2:]
    th, tw = output_size
    i = round((h - th) / 2.0)
    j = round((w - tw) / 2.0)
    return video[..., i:(i + th), j:(j + tw)]


def read_video_per_second(path: str) -> torch.Tensor:
    """
    Read video from path and return as a [T, H, W, C] tensor at native fps.
    """
    try:
        from torchvision.io import read_video as tv_read_video
        return tv_read_video(str(path))[0]
    except (ImportError, AttributeError):
        try:
            from torchcodec.decoders import VideoDecoder
            return VideoDecoder(str(path), dimension_order="NHWC").get_all_frames().data
        except ImportError:
            import cv2
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                raise ValueError(f"Cannot open video file: {path}")

            frames = []
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                frames.append(torch.from_numpy(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))

            cap.release()

            if not frames:
                raise ValueError(f"No frames read from video: {path}")

            return torch.stack(frames)


def preprocess_video_kinetics_400(input_video: torch.Tensor) -> torch.Tensor:
    """
    Preprocess input video for Kinetics-400 classification (ResNet-3D / R2+1D / MC3).

    Input:  [T, H, W, C] uint8 tensor
    Output: [C, T, 112, 112] float32 tensor in [0, 1]
    """
    input_video = _normalize_video(input_video)
    input_video = _resize_video(input_video, (128, 171))
    return _crop_video(input_video, (112, 112))

########################################################################


MODEL_ID = "mn1v138rn"
MODEL_NAME = "resnet_3d"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"


###############################################################

execution_ws = Path(os.path.dirname(os.path.abspath(__file__)))
model_dir = execution_ws / "models"
model_path = model_dir /  "{}.bin".format(MODEL_NAME)

input_video_path = execution_ws / "input.mp4"
INPUT_VIDEO_PATH_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/resnet_3d/v1/surfing_cutback.mp4"

########################################################################

SOC_ID = None
cleaned_argv = []
i = 0
while i < len(sys.argv):
    if sys.argv[i] == '--chipset':
        SOC_ID = sys.argv[i + 1]
        i += 2
    else:
        cleaned_argv.append(sys.argv[i])
        i += 1

sys.argv = cleaned_argv

print(f"SOC_ID: {SOC_ID}")

resnet_3d=None

# RESNET_3D class which inherited from the class QNNContext.
class ResNet_3D(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)[0]
        return output_data

def get_class_name_kinetics_400() -> list[str]:
    """Return the class name."""
    actions = "abseiling,air drumming,answering questions,applauding,applying cream,archery,arm wrestling,arranging flowers,assembling computer,auctioning,baby waking up,baking cookies,balloon blowing,bandaging,barbequing,bartending,beatboxing,bee keeping,belly dancing,bench pressing,bending back,bending metal,biking through snow,blasting sand,blowing glass,blowing leaves,blowing nose,blowing out candles,bobsledding,bookbinding,bouncing on trampoline,bowling,braiding hair,breading or breadcrumbing,breakdancing,brush painting,brushing hair,brushing teeth,building cabinet,building shed,bungee jumping,busking,canoeing or kayaking,capoeira,carrying baby,cartwheeling,carving pumpkin,catching fish,catching or throwing baseball,catching or throwing frisbee,catching or throwing softball,celebrating,changing oil,changing wheel,checking tires,cheerleading,chopping wood,clapping,clay pottery making,clean and jerk,cleaning floor,cleaning gutters,cleaning pool,cleaning shoes,cleaning toilet,cleaning windows,climbing a rope,climbing ladder,climbing tree,contact juggling,cooking chicken,cooking egg,cooking on campfire,cooking sausages,counting money,country line dancing,cracking neck,crawling baby,crossing river,crying,curling hair,cutting nails,cutting pineapple,cutting watermelon,dancing ballet,dancing charleston,dancing gangnam style,dancing macarena,deadlifting,decorating the christmas tree,digging,dining,disc golfing,diving cliff,dodgeball,doing aerobics,doing laundry,doing nails,drawing,dribbling basketball,drinking,drinking beer,drinking shots,driving car,driving tractor,drop kicking,drumming fingers,dunking basketball,dying hair,eating burger,eating cake,eating carrots,eating chips,eating doughnuts,eating hotdog,eating ice cream,eating spaghetti,eating watermelon,egg hunting,exercising arm,exercising with an exercise ball,extinguishing fire,faceplanting,feeding birds,feeding fish,feeding goats,filling eyebrows,finger snapping,fixing hair,flipping pancake,flying kite,folding clothes,folding napkins,folding paper,front raises,frying vegetables,garbage collecting,gargling,getting a haircut,getting a tattoo,giving or receiving award,golf chipping,golf driving,golf putting,grinding meat,grooming dog,grooming horse,gymnastics tumbling,hammer throw,headbanging,headbutting,high jump,high kick,hitting baseball,hockey stop,holding snake,hopscotch,hoverboarding,hugging,hula hooping,hurdling,hurling (sport),ice climbing,ice fishing,ice skating,ironing,javelin throw,jetskiing,jogging,juggling balls,juggling fire,juggling soccer ball,jumping into pool,jumpstyle dancing,kicking field goal,kicking soccer ball,kissing,kitesurfing,knitting,krumping,laughing,laying bricks,long jump,lunge,making a cake,making a sandwich,making bed,making jewelry,making pizza,making snowman,making sushi,making tea,marching,massaging back,massaging feet,massaging legs,massaging person's head,milking cow,mopping floor,motorcycling,moving furniture,mowing lawn,news anchoring,opening bottle,opening present,paragliding,parasailing,parkour,passing American football (in game),passing American football (not in game),peeling apples,peeling potatoes,petting animal (not cat),petting cat,picking fruit,planting trees,plastering,playing accordion,playing badminton,playing bagpipes,playing basketball,playing bass guitar,playing cards,playing cello,playing chess,playing clarinet,playing controller,playing cricket,playing cymbals,playing didgeridoo,playing drums,playing flute,playing guitar,playing harmonica,playing harp,playing ice hockey,playing keyboard,playing kickball,playing monopoly,playing organ,playing paintball,playing piano,playing poker,playing recorder,playing saxophone,playing squash or racquetball,playing tennis,playing trombone,playing trumpet,playing ukulele,playing violin,playing volleyball,playing xylophone,pole vault,presenting weather forecast,pull ups,pumping fist,pumping gas,punching bag,punching person (boxing),push up,pushing car,pushing cart,pushing wheelchair,reading book,reading newspaper,recording music,riding a bike,riding camel,riding elephant,riding mechanical bull,riding mountain bike,riding mule,riding or walking with horse,riding scooter,riding unicycle,ripping paper,robot dancing,rock climbing,rock scissors paper,roller skating,running on treadmill,sailing,salsa dancing,sanding floor,scrambling eggs,scuba diving,setting table,shaking hands,shaking head,sharpening knives,sharpening pencil,shaving head,shaving legs,shearing sheep,shining shoes,shooting basketball,shooting goal (soccer),shot put,shoveling snow,shredding paper,shuffling cards,side kick,sign language interpreting,singing,situp,skateboarding,ski jumping,skiing (not slalom or crosscountry),skiing crosscountry,skiing slalom,skipping rope,skydiving,slacklining,slapping,sled dog racing,smoking,smoking hookah,snatch weight lifting,sneezing,sniffing,snorkeling,snowboarding,snowkiting,snowmobiling,somersaulting,spinning poi,spray painting,spraying,springboard diving,squat,sticking tongue out,stomping grapes,stretching arm,stretching leg,strumming guitar,surfing crowd,surfing water,sweeping floor,swimming backstroke,swimming breast stroke,swimming butterfly stroke,swing dancing,swinging legs,swinging on something,sword fighting,tai chi,taking a shower,tango dancing,tap dancing,tapping guitar,tapping pen,tasting beer,tasting food,testifying,texting,throwing axe,throwing ball,throwing discus,tickling,tobogganing,tossing coin,tossing salad,training dog,trapezing,trimming or shaving beard,trimming trees,triple jump,tying bow tie,tying knot (not on a tie),tying tie,unboxing,unloading truck,using computer,using remote controller (not gaming),using segway,vault,waiting in line,walking the dog,washing dishes,washing feet,washing hair,washing hands,water skiing,water sliding,watering plants,waxing back,waxing chest,waxing eyebrows,waxing legs,weaving basket,welding,whistling,windsurfing,wrapping present,wrestling,writing,yawning,yoga,zumba"
    return actions.split(",")


def recognize_action_kinetics_400(prediction: torch.Tensor) -> list[str]:
    """
    Return the top 5 class names.
    Parameters:
        prediction: Get the probability for all classes.

    Returns:
        classnames: List of class ids from Kinetics-400 dataset is returned.

    """
    # Get top 5 class probabilities
    prediction = torch.topk(prediction.flatten(), 5).indices

    actions = get_class_name_kinetics_400()
    return [actions[pred] for pred in prediction]


def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(SOC_ID, MODEL_NAME, model_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Init():
    global resnet_3d

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for resnet_3d objects.
    resnet_3d= ResNet_3D("resnet_3d", str(model_path))


def preprocess_video(input_video):
    # input_video = input_video[:16]
    preprocessed_video = preprocess_video_kinetics_400(input_video)
    preprocessed_video =  preprocessed_video.unsqueeze(0).numpy()#
    preprocessed_video = np.transpose(preprocessed_video, (0, 2, 3,4,1))
    return  preprocessed_video


def post_process(predictions):
    top5_classes  = recognize_action_kinetics_400(predictions)
    top5_classes_str = ", ".join(top5_classes)
    print(f"Top 5 predictions:\n{top5_classes_str}\n")
    return top5_classes_str


    

def Inference(input_video_path):

    #Load video
    input_video = read_video_per_second(input_video_path)
    input_video = preprocess_video(input_video)


    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_data = resnet_3d.Inference(input_video)

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()


    # show the Top 5 predictions for image
    raw_prediction = torch.from_numpy(output_data)  

    result=post_process(raw_prediction)

    return result

def Release():
    global resnet_3d

    # Release the resources.
    del(resnet_3d)

def main(input = None):

    if input is None:
        if not os.path.exists(input_video_path):
            ret = True
            ret = install.download_url(INPUT_VIDEO_PATH_URL, input_video_path)
        input = input_video_path

    Init()

    result = Inference(input)

    Release()

    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a single video path.")
    parser.add_argument('--video', help='Path to the video', default=None)
    args = parser.parse_args()

    main(args.video)


