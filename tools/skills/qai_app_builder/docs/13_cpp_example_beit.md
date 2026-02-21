# C++ 示例：图像分类 (BEiT)

#### 示例 2：图像分类（BEiT）

基于真实示例代码：`samples/C++/beit/beit.cpp`

```cpp
#include "LibAppBuilder.hpp"
#include <iostream>
#include <vector>
#include <fstream>
#include <filesystem>
#include <opencv2/opencv.hpp>
#include <xtensor/xarray.hpp>
#include <xtensor/xadapt.hpp>
#include <xtensor/xmath.hpp>

namespace fs = std::filesystem;

const int IMAGENET_DIM = 224;

// ============================================
// Softmax 函数
// ============================================
xt::xarray<float> softmax(const xt::xarray<float>& x, std::size_t dim) {
    xt::xarray<float> exp_x = xt::exp(x);
    xt::xarray<float> sum_exp = xt::sum(exp_x, {dim}, xt::keep_dims);
    return exp_x / sum_exp;
}

// ============================================
// 转换 OpenCV Mat 为 xtensor（NCHW 格式）
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
    std::vector<int> shape = {img.cols, img.rows, img.channels()};

    std::vector<int> reshape_scale = {b, h, scale, w, scale, ch};
    std::vector<int> reshape_final = {b, out_channel, h, w};

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

// ============================================
// 预处理图像（ImageNet 标准）
// ============================================
xt::xarray<float> preprocess_image(const cv::Mat& image) {
    cv::Mat rgb_image;
    int scale = 1;

    // 转换为 RGB
    if (image.channels() == 3) {
        cv::cvtColor(image, rgb_image, cv::COLOR_BGR2RGB);
    } else {
        cv::cvtColor(image, rgb_image, cv::COLOR_GRAY2RGB);
    }

    // 1. 调整大小到 256x256
    cv::Mat resized_image;
    cv::resize(rgb_image, resized_image, cv::Size(256, 256), 0, 0, cv::INTER_LINEAR);

    // 2. 中心裁剪到 224x224
    int crop_x = (256 - IMAGENET_DIM) / 2;
    int crop_y = (256 - IMAGENET_DIM) / 2;
    cv::Rect roi(crop_x, crop_y, IMAGENET_DIM, IMAGENET_DIM);
    cv::Mat cropped_image = resized_image(roi).clone();

    // 3. 归一化到 [0, 1]
    cv::Mat input_mat;
    cropped_image.convertTo(input_mat, CV_32FC3, 1.0 / 255.0);

    // 4. 转换为 NCHW 格式
    xt::xarray<float> input_tensor = ConvertTensor(input_mat, scale);

    return input_tensor;
}

// ============================================
// BEiT 分类器类
// ============================================
class BEIT {
public:
    std::string model_name = "beit";
    std::string model_path;
    std::string backend_lib;
    std::string system_lib;
    LibAppBuilder libAppBuilder;

    BEIT(const std::string& model_path,
         const std::string& backend_lib,
         const std::string& system_lib)
        : model_path(model_path),
          backend_lib(backend_lib),
          system_lib(system_lib) {}

    int LoadModel() {
        std::cout << "正在加载模型..." << std::endl;
        int ret = libAppBuilder.ModelInitialize(
            model_name,
            model_path,
            backend_lib,
            system_lib,
            false  // async
        );
        std::cout << "模型加载完成" << std::endl;
        return ret;
    }

    int DestroyModel() {
        std::cout << "正在销毁模型..." << std::endl;
        int ret = libAppBuilder.ModelDestroy(model_name);
        return ret;
    }

    xt::xarray<float> predict(const cv::Mat& image) {
        std::cout << "正在预测..." << std::endl;

        int size = 3 * IMAGENET_DIM * IMAGENET_DIM;
        std::unique_ptr<float[]> input_buffer(new float[size]);

        // 预处理图像
        xt::xarray<float> input_tensor = preprocess_image(image);
        std::copy(input_tensor.begin(), input_tensor.end(), input_buffer.get());

        // 准备输入输出缓冲区
        std::vector<uint8_t*> inputBuffers;
        std::vector<uint8_t*> outputBuffers;
        std::vector<size_t> outputSize;
        std::string perfProfile = "burst";
        int graphIndex = 0;

        inputBuffers.push_back(reinterpret_cast<uint8_t*>(input_buffer.get()));

        // 执行推理
        std::cout << "正在执行推理..." << std::endl;
        int ret = libAppBuilder.ModelInference(
            model_name,
            inputBuffers,
            outputBuffers,
            outputSize,
            perfProfile,
            graphIndex
        );
        std::cout << "推理完成" << std::endl;

        if (ret < 0) {
            std::cout << "推理失败" << std::endl;
            return xt::zeros<float>({1000});
        }

        // 处理输出
        float* pred_output = reinterpret_cast<float*>(outputBuffers[0]);
        size_t output_elements = 1000;  // ImageNet 类别数

        xt::xarray<float> output = xt::zeros<float>({output_elements});
        std::copy(pred_output, pred_output + output_elements, output.begin());

        // 释放输出缓冲区
        for (auto buffer : outputBuffers) {
            free(buffer);
        }

        // 应用 softmax
        return softmax(output, 0);
    }
};

// ============================================
// 加载 ImageNet 标签
// ============================================
std::vector<std::string> load_labels(const std::string& file_path) {
    std::vector<std::string> labels;
    std::ifstream file(file_path);

    if (file.is_open()) {
        std::string line;
        while (std::getline(file, line)) {
            labels.push_back(line);
        }
        file.close();
    }

    return labels;
}

// ============================================
// 主函数
// ============================================
int main() {
    // 设置路径
    std::string image_path = "../input.jpg";
    std::string json_path = "../models/imagenet_labels.json";
    std::string model_path = "../models/beit.bin";
    std::string backend_lib = "../qai_libs/QnnHtp.dll";
    std::string system_lib = "../qai_libs/QnnSystem.dll";

    // 读取图像
    cv::Mat image = cv::imread(image_path);
    if (image.empty()) {
        std::cout << "无法读取图像" << std::endl;
        return -1;
    }

    // 创建分类器
    BEIT beit(model_path, backend_lib, system_lib);

    // 设置日志级别
    SetLogLevel(QNN_LOG_LEVEL_WARN);

    // 加载模型
    int ret = beit.LoadModel();
    if (ret < 0) {
        std::cout << "模型加载失败" << std::endl;
        return -1;
    }

    // 执行预测
    xt::xarray<float> probabilities = beit.predict(image);
    std::cout << "预测完成，概率数组大小: " << probabilities.size() << std::endl;

    // 找到 Top-5 预测
    std::vector<std::pair<float, int>> indexed_probs;
    for (size_t i = 0; i < probabilities.size(); ++i) {
        indexed_probs.emplace_back(probabilities[i], static_cast<int>(i));
    }
    std::sort(indexed_probs.begin(), indexed_probs.end(),
              std::greater<std::pair<float, int>>());

    // 加载标签
    std::vector<std::string> labels = load_labels(json_path);

    // 打印 Top-5 结果
    std::cout << "\nTop 5 预测结果:" << std::endl;
    for (int i = 0; i < 5; ++i) {
        int class_idx = indexed_probs[i].second;
        float prob = indexed_probs[i].first;
        std::string label = (class_idx < labels.size()) ? labels[class_idx] : "Unknown";
        std::cout << (i + 1) << ". " << label << ": " 
                  << (100 * prob) << "%" << std::endl;
    }

    // 销毁模型
    ret = beit.DestroyModel();
    if (ret < 0) {
        std::cout << "模型销毁失败" << std::endl;
        return -1;
    }

    return 0;
}
```

