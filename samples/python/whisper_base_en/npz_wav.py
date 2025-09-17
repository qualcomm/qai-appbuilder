# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import numpy as np
from scipy.io.wavfile import write

# 加载NPZ文件
data = np.load('jfk.npz')
print("列出文件中所有可用的键", data.files)

array = data['audio']  # 使用实际的数组名称

# 归一化数据到[-1, 1]范围
normalized_data = array / np.max(np.abs(array))

# 将归一化数据转换为16位整数
audio_data = np.int16(normalized_data * 32767)

# 保存为WAV文件
write('jfk.wav', 16000, audio_data)

print("转换完成，音频文件已保存为 jfk.wav")