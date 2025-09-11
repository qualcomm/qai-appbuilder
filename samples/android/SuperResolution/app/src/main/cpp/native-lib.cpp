#include <jni.h>
#include <string>
#include <iostream>
#include <filesystem>
#include <string>
#include <vector>
#include <cstddef>

#include <opencv2/opencv.hpp>
#include <xtensor/xtensor.hpp>
#include <xtensor/xarray.hpp>
#include <xtensor/xadapt.hpp>
#include <xtensor/xnpy.hpp>
#include <LibAppBuilder.hpp>

#ifdef __ANDROID__
#include <android/log.h>
#define LOG_TAG "com.example.genieapiservice"
#define LOGD(...) __android_log_print(ANDROID_LOG_DEBUG, LOG_TAG, __VA_ARGS__)
void My_Log(const std::string& message) {
    LOGD("%s", message.c_str());
}
#else
void My_Log(const std::string& message) {
    std::cout << message << std::endl;
}
#endif

namespace fs = std::filesystem;
const std::string MODEL_NAME = "real_esrgan_x4plus";
const int IMAGE_SIZE = 128;
int IMAGE_WIDTH = IMAGE_SIZE;
int IMAGE_HEIGHT = IMAGE_SIZE;
const int scale = 4;

#define RGB_IMAGE_SIZE_F32(width, height) ((width) * (height) * 3 * 4)

xt::xarray<float> ConvertTensor(cv::Mat &img, int scale)
{
    int b = 1;
    int ch = img.channels();
    int hh = img.rows;
    int hw = img.cols;
    int out_channel = ch * (scale * scale);
    int h = hh / scale;
    int w = hw / scale;

    // input img is NHWC
    size_t size = img.total();
    size_t channels = img.channels();
    std::vector<int> shape = {img.rows, img.cols, img.channels()};
    std::vector<int> reshape_scale = {b, h, scale, w, scale, ch};
    std::vector<int> reshape_final = {b, h, w, out_channel};

    // convert to xarray
    xt::xarray<float> input = xt::adapt((float *)img.data, size * channels, xt::no_ownership(), shape);
    input.reshape(reshape_scale);
    input = xt::transpose(input, {0, 1, 3, 5, 2, 4});
    input.reshape(reshape_final);

    return input;
}

int super_resolution(std::string libs_path, std::string model_path, std::string input_img, std::string output_img) {

    std::string  backend_lib_path = libs_path + "/libQnnHtp.so";
    std::string  system_lib_path = libs_path + "/libQnnSystem.so";
    std::string  input_path = input_img;
    std::string  output_path = output_img;

    LibAppBuilder libAppBuilder;
    SetLogLevel(2);          //LogLevel::WARN
    SetProfilingLevel(1);    //ProfilingLevel::BASIC

    My_Log("ModelInitialize");
    int ret = libAppBuilder.ModelInitialize(MODEL_NAME, model_path, backend_lib_path, system_lib_path);
    My_Log("ModelInitialize done");
    if(ret == false){
        My_Log("LoadModel failed");
        return 0;
    }
    else {
        My_Log("LoadModel success");
    }

    // Fill the inputBuffers before inference.
    cv::Mat rgb_image;
    cv::Mat orig_image = cv::imread(input_path, cv::IMREAD_COLOR);
    cv::Mat image;

    cv::resize(orig_image, image, cv::Size(IMAGE_WIDTH, IMAGE_HEIGHT), 0, 0, cv::INTER_LINEAR);

    //cv::imshow("orig Image", orig_image);
    //cv::waitKey(0);

    int modelWidth = IMAGE_WIDTH;
    int modelHeight = IMAGE_HEIGHT;
    int srWidth = scale * modelWidth;
    int srHeight = scale * modelHeight;

    uint32_t size = RGB_IMAGE_SIZE_F32(modelWidth, modelHeight);
    uint8_t *nchwBufLeft = new uint8_t[size];
    cv::Mat inputMat(modelHeight, modelWidth, CV_32FC3, nchwBufLeft);

    float *dest = new float[size]; // siez???
    cv::cvtColor(image, rgb_image, cv::COLOR_BGR2RGB); // 可选：BGR→RGB转换
    rgb_image.convertTo(inputMat, CV_32FC3, 1.0 / 255.0);

    //cv::imshow("processed Image", inputMat);
    //cv::waitKey(0);

    xt::xarray<float> input_tensor = ConvertTensor(inputMat, 1);
    std::copy(input_tensor.begin(), input_tensor.end(), dest);

    SetPerfProfileGlobal("burst");

    std::vector<uint8_t*> inputBuffers;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;

    std::string perfProfile = "burst";//default

    inputBuffers.clear();
    outputBuffers.clear();
    outputSize.clear();

    inputBuffers.push_back((uint8_t *)dest);

    My_Log("ModelInference");
    ret = libAppBuilder.ModelInference(MODEL_NAME, inputBuffers, outputBuffers, outputSize, perfProfile);
    My_Log("ModelInference done");
    if(ret == false){
        My_Log("ModelInference failed");
        return 0;
    }
    else {
        My_Log("ModelInference success");
    }

    // Use the data in outputBuffers.
    RelPerfProfileGlobal();

    My_Log("outputBuffers" + std::to_string(outputBuffers.size()));

    delete[] nchwBufLeft;
    delete[] dest;

    float *predOutput = (float *)outputBuffers.at(0);

    // outpus shape is NHWC
    int outpuChannel = 3;
    int outputTensorSize = srWidth * srHeight * outpuChannel;
    float val;
    float m = 0.0;
    char *buffer = new char[outputTensorSize];
    for (int i = 0; i < outputTensorSize; i++)
    {
        val = predOutput[i];
        buffer[i] = std::max(0.0, std::min(255.0, (val * 255.0)));
    }

    cv::Mat outputMat(srHeight, srWidth, CV_8UC3, buffer);
    cv::Mat outputMatBgr;
    cv::cvtColor(outputMat, outputMatBgr, cv::COLOR_BGR2RGB);

    std::cout << "output shape: "
              << outputMat.size[0] << " x "  // height
              << outputMat.size[1] << std::endl;  // width

    // cv::imshow("output Image", outputMatBgr);
    cv::imwrite(output_path, outputMatBgr);
    // cv::waitKey(0);

    // Free the memory in outputBuffers.
    for (int j = 0; j < outputBuffers.size(); j++) {
        free(outputBuffers[j]);
    }
    outputBuffers.clear();
    outputSize.clear();

    libAppBuilder.ModelDestroy(MODEL_NAME);

    return 0;
}

int main(int argc, char* argv[]) {
    std::string libs_dir = std::string(argv[1]);
    std::string model_name = std::string(argv[2]);
    std::string input_img = std::string(argv[3]);
    std::string output_img = std::string(argv[4]);

    super_resolution(libs_dir, model_name, input_img, output_img);
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_example_superresolution_MainActivity_SuperResolution(
        JNIEnv* env,
        jobject /* this */, jstring j_libsDir, jstring j_model_path, jstring j_input_img, jstring j_output_img) {

    const char* libs_dir_cstr = env->GetStringUTFChars(j_libsDir, nullptr);
    const char* model_path_cstr = env->GetStringUTFChars(j_model_path, nullptr);
    const char* input_img_cstr = env->GetStringUTFChars(j_input_img, nullptr);
    const char* output_img_cstr = env->GetStringUTFChars(j_output_img, nullptr);

    std::string libs_dir(libs_dir_cstr);
    std::string model_name(model_path_cstr);
    std::string input_img(input_img_cstr);
    std::string output_img(output_img_cstr);

    env->ReleaseStringUTFChars(j_libsDir, libs_dir_cstr);
    env->ReleaseStringUTFChars(j_model_path, model_path_cstr);
    env->ReleaseStringUTFChars(j_input_img, input_img_cstr);
    env->ReleaseStringUTFChars(j_output_img, output_img_cstr);

    My_Log("Java_com_example_superresolution_MainActivity_SuperResolution");
    super_resolution(libs_dir, model_name, input_img, output_img);
    My_Log("Java_com_example_superresolution_MainActivity_SuperResolution end.");

    std::string sample = "RealESRGAN Sample";

    return env->NewStringUTF(sample.c_str());
}
