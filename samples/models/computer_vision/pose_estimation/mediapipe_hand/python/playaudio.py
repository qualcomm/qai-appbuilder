# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import pygame
import time
from enum import Enum

class AudioState(Enum):
    PLAY = 1
    PAUSE = 2
    STOP = 3

audio_state = AudioState.STOP
current_pos = 0
audio_length = 0
time_save = 0

def load_audio(
    file_path: str
) -> bool:
    global audio_length

    if file_path is None:
        return False
    
    pygame.mixer.init()

    pygame.mixer.music.load(file_path)
    sound = pygame.mixer.Sound(file_path)
    audio_length = sound.get_length()

    time_save = time.time()

    if audio_length > 0:
        return True
    else:
        return False
    
def play():
    global current_pos, audio_state

    if audio_state == AudioState.PAUSE:
        pygame.mixer.music.unpause()
        audio_state = AudioState.PLAY
    elif audio_state == AudioState.STOP:
        pygame.mixer.music.play(start=current_pos)
        audio_state = AudioState.PLAY
    else:
        return

def pause():
    global audio_state, current_pos

    if audio_state == AudioState.PLAY:
        pygame.mixer.music.pause()
        audio_state = AudioState.PAUSE
        current_pos = pygame.mixer.music.get_pos()
    else:
        return

def stop():
    global current_pos, audio_state

    if audio_state is not AudioState.STOP:
        pygame.mixer.music.stop()
        current_pos = 0
        audio_state = AudioState.STOP
    else:
        return

def seek(
    offset_set: int
):
    global current_pos, audio_state, time_save

    time_now = time.time()

    if audio_state is not AudioState.PLAY or offset_set == 0 or time_now - time_save < 5:
        return

    time_save = time_now

    new_pos = pygame.mixer.music.get_pos() / 1000 + offset_set
    current_pos = max(0, min(new_pos, audio_length))

    if pygame.mixer.music.get_busy():
        pygame.mixer.music.set_pos(current_pos)

def quit():
    pygame.mixer.music.stop()
    pygame.mixer.music.unload()
    pygame.mixer.quit()
    pygame.quit()