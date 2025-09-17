//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "LibAppBuilder.hpp"
#include <iostream>
#include <filesystem>
#include <string>
#include <vector>
#include <cstddef>
#include <opencv2/opencv.hpp>
#include <opencv2/core/utils/logger.hpp>
#include <xtensor.hpp>

namespace fs = std::filesystem;
const std::string MODEL_NAME = "real_esrgan_x4plus";
const int IMAGE_SIZE = 512;
int IMAGE_WIDTH = 512;
int IMAGE_HEIGHT = 512;
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

int main() {
	
	fs::path execution_ws = fs::current_path();
	fs::path backend_lib_path = execution_ws / "QnnHtp.dll";
	fs::path system_lib_path = execution_ws / "QnnSystem.dll";
    fs::path model_path = execution_ws / (MODEL_NAME + ".bin");
    fs::path input_path = execution_ws / "input.jpg";
	fs::path output_path = execution_ws / "output.jpg";

	LibAppBuilder libAppBuilder;
	SetLogLevel(2);          //LogLevel::WARN
	SetProfilingLevel(1);    //ProfilingLevel::BASIC

    std::cout << "ModelInitialize" << std::endl;
	int ret = libAppBuilder.ModelInitialize(MODEL_NAME, model_path.string(), backend_lib_path.string(), system_lib_path.string());
	std::cout << "ModelInitialize done" << std::endl;
	if(ret < 0){
        std::cout << "LoadModel failed" << std::endl;
        return 0;
    }

    // Fill the inputBuffers before inference.
    cv::Mat rgb_image;
	cv::Mat orig_image = cv::imread(input_path.string(), cv::IMREAD_COLOR);

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
	cv::cvtColor(orig_image, rgb_image, cv::COLOR_BGR2RGB);
	rgb_image.convertTo(inputMat, CV_32FC3, 1.0 / 255.0);

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
	
    
	
	std::cout << "ModelInference" << std::endl;   
    ret = libAppBuilder.ModelInference(MODEL_NAME, inputBuffers, outputBuffers, outputSize, perfProfile);
	std::cout << "ModelInference done" << std::endl;
	if(ret < 0){
        std::cout << "ModelInference failed" << std::endl;
        return 0;
    }

    // Use the data in outputBuffers.
    RelPerfProfileGlobal();
	
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
        // val=(val+1)/2;
        buffer[i] = std::max(0.0, std::min(255.0, (val * 255.0)));
    }

    cv::Mat outputMat(srHeight, srWidth, CV_8UC3, buffer);
    cv::Mat outputMatBgr;
    cv::cvtColor(outputMat, outputMatBgr, cv::COLOR_BGR2RGB);
	
	std::cout << "output shape: " 
              << outputMat.size[0] << " x "  // height
              << outputMat.size[1] << std::endl;  // width
			  
	cv::imshow("output Image", outputMatBgr);
	cv::imwrite(output_path.string(), outputMatBgr);
    cv::waitKey(0);
	
    // Free the memory in outputBuffers.
    for (int j = 0; j < outputBuffers.size(); j++) {
        free(outputBuffers[j]);
    }
    outputBuffers.clear();
    outputSize.clear();

    libAppBuilder.ModelDestroy(MODEL_NAME);

    return 0;
}