# C++ 示例：图像超分辨率

### 4.3 完整 C++ 示例

#### 示例 1：图像超分辨率（Real-ESRGAN）

基于真实示例代码：`samples/C++/real_esrgan_x4plus/real_esrgan_x4plus.cpp`

```cpp
#include "LibAppBuilder.hpp"
#include <iostream>
#include <filesystem>
#include <opencv2/opencv.hpp>
#include <xtensor/xarray.hpp>
#include <xtensor/xadapt.hpp>

namespace fs = std::filesystem;

const std::string MODEL_NAME = "real_esrgan_x4plus";
const int IMAGE_SIZE = 512;
const int SCALE = 4;

#define RGB_IMAGE_SIZE_F32(width, height) ((width) * (height) * 3 * 4)

// ============================================
// 辅助函数：转换 OpenCV Mat 为 xtensor
// ============================================
xt::xarray<float> ConvertTensor(cv::Mat &img, int scale) {
    int b = 1;
    int ch = img.channels();
    int hh = img.rows;
    int hw = img.cols;
    int out_channel = ch * (scale * scale);
    int h = hh / scale;
    int w = hw / scale;

    // 输入 img 是 HWC 格式
    size_t size = img.total();
    size_t channels = img.channels();
    std::vector<int> shape = {img.rows, img.cols, img.channels()};

    std::vector<int> reshape_scale = {b, h, scale, w, scale, ch};
    std::vector<int> reshape_final = {b, h, w, out_channel};

    // 转换为 xarray
    xt::xarray<float> input = xt::adapt(
        (float*)img.data, 
        size * channels, 
        xt::no_ownership(), 
        shape
    );

    input.reshape(reshape_scale);
    input = xt::transpose(input, {0, 1, 3, 5, 2, 4});
    input.reshape(reshape_final);

    return input;
}

int main() {
    // ============================================
    // 1. 设置路径
    // ============================================
    fs::path execution_ws = fs::current_path();
    fs::path backend_lib_path = execution_ws / "QnnHtp.dll";
    fs::path system_lib_path = execution_ws / "QnnSystem.dll";
    fs::path model_path = execution_ws / (MODEL_NAME + ".bin");
    fs::path input_path = execution_ws / "input.jpg";
    fs::path output_path = execution_ws / "output.jpg";

    // ============================================
    // 2. 初始化日志和性能分析
    // ============================================
    SetLogLevel(QNN_LOG_LEVEL_WARN);
    SetProfilingLevel(QNN_PROFILING_LEVEL_BASIC);

    // ============================================
    // 3. 创建 LibAppBuilder 实例并初始化模型
    // ============================================
    LibAppBuilder libAppBuilder;

    std::cout << "正在初始化模型..." << std::endl;
    int ret = libAppBuilder.ModelInitialize(
        MODEL_NAME,
        model_path.string(),
        backend_lib_path.string(),
        system_lib_path.string()
    );

    if (ret < 0) {
        std::cout << "模型加载失败" << std::endl;
        return -1;
    }
    std::cout << "模型初始化完成" << std::endl;

    // ============================================
    // 4. 读取和预处理图像
    // ============================================
    cv::Mat orig_image = cv::imread(input_path.string(), cv::IMREAD_COLOR);
    if (orig_image.empty()) {
        QNN_ERR("无法读取图像: %s", input_path.string().c_str());
        return -1;
    }

    // 转换为 RGB
    cv::Mat rgb_image;
    cv::cvtColor(orig_image, rgb_image, cv::COLOR_BGR2RGB);

    // 调整大小
    cv::Mat resized_image;
    cv::resize(rgb_image, resized_image, cv::Size(IMAGE_SIZE, IMAGE_SIZE));

    // 归一化到 [0, 1]
    cv::Mat input_mat;
    resized_image.convertTo(input_mat, CV_32FC3, 1.0 / 255.0);

    // 转换为模型输入格式
    xt::xarray<float> input_tensor = ConvertTensor(input_mat, 1);

    // 分配输入缓冲区
    uint32_t size = RGB_IMAGE_SIZE_F32(IMAGE_SIZE, IMAGE_SIZE);
    float* input_buffer = new float[size / 4];
    std::copy(input_tensor.begin(), input_tensor.end(), input_buffer);

    // ============================================
    // 5. 执行推理
    // ============================================
    std::vector<uint8_t*> inputBuffers;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    std::string perfProfile = "burst";

    inputBuffers.push_back(reinterpret_cast<uint8_t*>(input_buffer));

    // 设置全局性能模式
    SetPerfProfileGlobal("burst");

    std::cout << "正在执行推理..." << std::endl;
    TimerHelper timer;

    ret = libAppBuilder.ModelInference(
        MODEL_NAME,
        inputBuffers,
        outputBuffers,
        outputSize,
        perfProfile
    );

    timer.Print("推理时间: ", false);

    // 释放性能模式
    RelPerfProfileGlobal();

    if (ret < 0) {
        std::cout << "推理失败" << std::endl;
        delete[] input_buffer;
        return -1;
    }
    std::cout << "推理完成" << std::endl;

    // ============================================
    // 6. 后处理
    // ============================================
    float* output_data = reinterpret_cast<float*>(outputBuffers[0]);

    int output_width = IMAGE_SIZE * SCALE;
    int output_height = IMAGE_SIZE * SCALE;
    int output_channels = 3;
    int output_tensor_size = output_width * output_height * output_channels;

    // 反归一化并转换为 uint8
    char* buffer = new char[output_tensor_size];
    for (int i = 0; i < output_tensor_size; i++) {
        float val = output_data[i];
        buffer[i] = std::max(0.0, std::min(255.0, val * 255.0));
    }

    // 创建输出图像
    cv::Mat output_mat(output_height, output_width, CV_8UC3, buffer);
    cv::Mat output_mat_bgr;
    cv::cvtColor(output_mat, output_mat_bgr, cv::COLOR_RGB2BGR);

    // 保存图像
    cv::imwrite(output_path.string(), output_mat_bgr);
    std::cout << "输出图像已保存到: " << output_path.string() << std::endl;

    // 显示图像（可选）
    cv::imshow("Output Image", output_mat_bgr);
    cv::waitKey(0);

    // ============================================
    // 7. 清理资源
    // ============================================
    delete[] input_buffer;
    delete[] buffer;

    // 释放输出缓冲区（重要！）
    for (size_t j = 0; j < outputBuffers.size(); j++) {
        free(outputBuffers[j]);
    }
    outputBuffers.clear();
    outputSize.clear();

    // 销毁模型
    libAppBuilder.ModelDestroy(MODEL_NAME);

    return 0;
}
```

