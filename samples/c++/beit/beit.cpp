#define NOMINMAX
#include <iostream>
#include <vector>
#include <string>
#include <fstream>
#include <sstream>
#include <algorithm>
#include <filesystem>
#include <opencv2/opencv.hpp>
#include <memory>
#include <LibAppBuilder.hpp>
#include <xtensor/containers/xarray.hpp>
#include <xtensor/io/xio.hpp>
#include <xtensor/containers/xadapt.hpp>
#include <xtensor/core/xmath.hpp>
#include <xtensor/core/xoperation.hpp>
#include <xtensor/core/xexpression.hpp> 

using namespace cv; 
using namespace std;
namespace fs = std::filesystem;

xt::xarray<float> softmax(const xt::xexpression<xt::xarray<float>>& e, std::size_t dim)
{
	std::cout << "softmax" << std::endl;
    // 获取输入张量
    xt::xarray<float> x = e.derived_cast();    
    // 直接计算指数
    xt::xarray<float> exp_x = xt::exp(x);    
    // 沿指定维度求和
    xt::xarray<float> sum_exp = xt::sum(exp_x, {dim}, xt::keep_dims);    
    // 计算softmax
	std::cout << "softmax done" << std::endl;
    return exp_x / sum_exp;
}

xt::xarray<float> ConvertTensor(cv::Mat &img,int scale)
{
  int b = 1;
  int ch = img.channels();
  int hh = img.rows;
  int hw = img.cols;
  int out_channel = ch * (scale*scale);
  int h = hh / scale;
  int w = hw /scale;

  //input img is NHWC
  size_t size = img.total();
  size_t channels = img.channels();
  std::vector<int> shape = {img.cols, img.rows, img.channels()};

  std::vector<int> reshape_scale={b,h,scale,w,scale,ch};
  std::vector<int> reshape_final={b,out_channel,h,w};

  //convert to xarray
  xt::xarray<float> input = xt::adapt((float*)img.data, size * channels, xt::no_ownership(), shape);
  std::cout<<"Input Shape="<<xt::adapt(input.shape()) << std::endl; //{224, 224,   3}
  input.reshape(reshape_scale);
  std::cout<<"Reshape Shape="<<xt::adapt(input.shape()) << std::endl; //{  1, 224,   1, 224,   1,   3}

  input=xt::transpose(input, {0,1,3,5,2,4});
  std::cout<<"Transpose Shape="<<xt::adapt(input.shape()) << std::endl; //{  1, 224, 224,   3,   1,   1}

  input.reshape(reshape_final);
  std::cout<<"Finale Shape="<<xt::adapt(input.shape()) << std::endl; //{  1,   3, 224, 224}

  return input;
}

const int IMAGENET_DIM = 224;

// 预处理图像
xt::xarray<float> preprocess_image(const cv::Mat& image) {
    // 确保图像是RGB格式
    cv::Mat rgbImage;
    int scale = 1;
    if (image.channels() == 3) {
        cv::cvtColor(image, rgbImage, cv::COLOR_BGR2RGB);
    } else {
        // 处理单通道图像（灰度图）
        cv::cvtColor(image, rgbImage, cv::COLOR_GRAY2RGB);
    }
    
    // 1. 调整大小到256x256
    cv::Mat resizedImage;
    cv::resize(rgbImage, resizedImage, cv::Size(256, 256), 0, 0, cv::INTER_LINEAR);
    
    // 2. 中心裁剪到224x224
    int crop_x = (256 - IMAGENET_DIM) / 2;
    int crop_y = (256 - IMAGENET_DIM) / 2;
    cv::Rect roi(crop_x, crop_y, IMAGENET_DIM, IMAGENET_DIM);
    cv::Mat croppedImage = resizedImage(roi).clone();

    cv::Mat inputMat; // 声明inputMat变量   
    croppedImage.convertTo(inputMat, CV_32FC3, 1.0/255.0);
    xt::xarray<float> input_tensor = ConvertTensor(inputMat,scale);
    return input_tensor;
}

class BEIT
{
public:
    std::string model_name = "beit";
    std::string model_path = "../models/beit-beit-qualcomm_snapdragon_x_elite-float.bin";
    std::string  backendLib = "../qai_libs/QnnHtp.dll";
    std::string  systemLib = "../qai_libs/QnnSystem.dll";
    std::string perf = "burst";
    LibAppBuilder LibAppBuilder;
    int graphIndex = 0;
    std::string excutePath;    
    std::vector<uint8_t*> inputBuffers;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;
    int modelHeight = 224;
    int modelWidth = 224;
    int size = 3 * modelHeight * modelWidth;
    int LoadModel(){
		std::cout << "ModelInitialize" << std::endl;
        int ret = LibAppBuilder.ModelInitialize(model_name, model_path, backendLib, systemLib, false);
		std::cout << "ModelInitialize done" << std::endl;
        return ret;
    }
    int DestroyModel(){
        std::cout << "DestroyModel" << std::endl;
        int ret = LibAppBuilder.ModelDestroy(model_name);
        return ret;
    }

    xt::xarray<float> predict(const cv::Mat& image) {
		std::cout << "predict" << std::endl;
        bool ret = 0;
        std::unique_ptr<float[]> nchwBufLeft(new float[size]); // 使用智能指针管理内存
		xt::xarray<float> output = xt::zeros<float>({1000});
		std::cout << "output.size " << output.size() << std::endl;
        xt::xarray<float> input_tensor = preprocess_image(image);

        std::copy(input_tensor.begin(), input_tensor.end(), nchwBufLeft.get());
    
        inputBuffers.clear();
        outputBuffers.clear();
        outputSize.clear();
        
        inputBuffers.push_back((uint8_t * )nchwBufLeft.get());
		std::cout << "ModelInference" << std::endl;        
        ret  = LibAppBuilder.ModelInference("beit", inputBuffers, outputBuffers, outputSize, perf, graphIndex);
        std::cout << "ModelInference done" << std::endl;  
        float* predOutput = (float*)outputBuffers.at(0);
        size_t output_elements = output.size();
        std::copy(predOutput, predOutput + output_elements, output.begin());
		std::cout << "output.size " << output.size() << std::endl;
        return softmax(output, 0);
    }
};

// 加载JSON文件
std::vector<std::string> load_json(const std::string& file_path) {
    std::cout << "load_json" << std::endl;
    std::vector<std::string> labels;
    std::ifstream file(file_path);
    if (file.is_open()) {
        std::string line;
        while (std::getline(file, line)) {
            // 简单解析JSON，假设每行是一个标签
            labels.push_back(line);
        }
        file.close();
    }
    return labels;
}

// 加载图像
cv::Mat load_image(const std::string& image_path) {
    std::cout << "load_image" << std::endl;
    return cv::imread(image_path);
}

// 运行Imagenet分类器演示
void imagenet_demo() {
    std::cout << "imagenet_demo" << std::endl;
    //char path[MAX_PATH];
    int ret = 0;
	std::string image_path = "../input.jpg";
	std::string json_path = "../models/imagenet_labels.json";
    BEIT Beit;
	xt::xarray<float> probabilities = xt::zeros<float>({1000});
    std::vector<std::pair<float, int>> indexed_probs;
    std::vector<int> top5_indices;
    std::vector<float> top5_values;
    std::vector<std::string> labels;
	  
   std::string fullPath = fs::current_path().string();
   std::cout << "Current working directory: " << fullPath << std::endl;


   // 查找最后一个反斜杠的位置   
    size_t pos = fullPath.find_last_of("\\/");
    // 获取目录路径
    std::string directory = fullPath.substr(0, pos)+"\\";
    
    cv::Mat image = load_image(image_path);
    
    SetLogLevel(2);

    std::cout << "LoadModel " << fullPath << std::endl;
    ret = Beit.LoadModel();
    if(ret < 0){
        std::cout << "LoadModel failed" << std::endl;
        return;
    }
    
    probabilities = Beit.predict(image);
	
    std::cout << "predict done, probabilities.size " << probabilities.size() <<std::endl;


    for (size_t i = 0; i < probabilities.size(); ++i) {
		//std::cout << probabilities[i] << std::endl;
        indexed_probs.emplace_back(probabilities[i], static_cast<int>(i));
    }
    std::sort(indexed_probs.begin(), indexed_probs.end(), std::greater<std::pair<float, int>>());

    // 找到top 5的索引和值
    for (int i = 0; i < 5; ++i) {
        top5_indices.push_back(indexed_probs[i].second);
        top5_values.push_back(indexed_probs[i].first);
    }

    labels = load_json(json_path);
    std::cout << "labels.size() = " << labels.size() << std::endl;
    std::cout << "Top 5 predictions for image:" << std::endl;
    for (int i = 0; i < 5; ++i) {
        std::cout << labels[top5_indices[i]] << ": " << 100 * top5_values[i] << "%" << std::endl;
    }
	
    //UnInitialization();
    ret = Beit.DestroyModel();
    if(ret < 0){
        std::cout << "DestroyModel failed" << std::endl;
        return;
    }
}

int main() {
    imagenet_demo();
    return 0;
}