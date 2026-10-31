//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <chrono>
#include <filesystem>

#include "log.h"
#include "utils.h"

namespace fs = std::filesystem;

struct Timer::Impl
{
    std::chrono::steady_clock::time_point time_start;
    std::chrono::steady_clock::time_point time_now;
};

Timer::Timer() : impl_{new Impl{}}
{
    Reset();
}

void Timer::Print(const std::string &message)
{
    impl_->time_now = std::chrono::steady_clock::now();
    double dr_ms = std::chrono::duration<double, std::milli>(impl_->time_now - impl_->time_start).count();
    My_Log{} << YELLOW << std::fixed << std::setprecision(2) << dr_ms
             << " ms" << RESET << std::endl;
}

void Timer::Print(std::string message, bool reset)
{
    Print(message);
    if (reset)
    {
        Reset();
    }
}

long long Timer::GetSystemTime()
{
    auto now = std::chrono::system_clock::now();
    auto duration = now.time_since_epoch();
    auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(duration).count();

    return milliseconds;
}

void Timer::Reset()
{
    impl_->time_start = std::chrono::steady_clock::now();
}

std::unique_ptr<int8_t[]> File::get_file_as_buffer(const std::string &file_path, uint32_t &size)
{
    std::ifstream in(file_path, std::ifstream::binary | std::ios::ate);
    size = in.tellg();
    if (size == 0)
        return nullptr;
    auto buf = std::make_unique<int8_t[]>(size);
    in.read(reinterpret_cast<char *>(buf.get()), size);
    return buf;
}

size_t File::get_file_size(const std::string &file_path, std::ios::openmode mode)
{
    std::ifstream file(file_path, mode | std::ios::ate);
    return file.tellg();
}

bool File::IsFileEmpty(const std::string &file_path)
{
    std::ifstream f;
    f.open(file_path.c_str(), std::ios::in | std::ios::binary | std::ios::ate);
    return f.tellg() == 0;
}

bool File::IsFileExist(const std::string &file_path)
{
    return std::ifstream(file_path.c_str()).good() || fs::is_directory(file_path);
}

std::string File::ResolveModelConfigPath(const std::string &model_dir)
{
    std::string genie_config_path = model_dir + "/genie_config.json";
    if (IsFileExist(genie_config_path))
    {
        return genie_config_path;
    }
    return model_dir + "/config.json";
}

std::string File::DeriveAncestorDir(const std::string &file_path, const std::string &base_dir, int levels)
{
    if (file_path.empty() || levels <= 0)
        return "";

    fs::path path{file_path};
    if (path.is_relative())
    {
        path = fs::path(base_dir) / path;
    }
    std::error_code ec;
    fs::path canonical_path = fs::weakly_canonical(path, ec);
    if (!ec)
    {
        path = canonical_path;
    }

    for (int i = 0; i < levels; ++i)
    {
        fs::path parent = path.parent_path();
        if (parent.empty() || parent == path)
        {
            return "";
        }
        path = parent;
    }
    return path.generic_string();
}

bool File::MatchFileInDir(const std::string &dir_path, const std::string &part, std::vector<std::string> *files)
{

    bool found = false;
    for (const auto &entry: fs::directory_iterator(dir_path))
    {
        auto file_path = entry.path().generic_string();
        if (entry.is_regular_file() && str_contains(file_path, part))
        {
            found = true;
            if (files)
            {
                files->push_back(file_path);
            }
            else
            {
                return true;
            }
        }
    }
    return found;
}

template<typename T>
std::vector<T> File::ReadFile(const std::string &file_name, bool binary)
{
    auto mode = binary ? std::ios::binary : std::ios::in;
    std::ifstream in(file_name, mode);
    auto file_size = get_file_size(file_name, mode);
    std::vector<T> buffer(file_size / sizeof(T));
    in.read(reinterpret_cast<char *>(buffer.data()), file_size);
    in.close();
    return buffer;
}

template std::vector<uint8_t> File::ReadFile(const std::string &file_name, bool binary);
template std::vector<float> File::ReadFile(const std::string &file_name, bool binary);
template std::vector<int> File::ReadFile(const std::string &file_name, bool binary);

template<typename T>
void File::WriteBinaryFile(const T *buf, int size, const std::string &file_name)
{
    std::ofstream out(file_name, std::ios::binary);
    out.write(reinterpret_cast<const char *>(buf), size * sizeof(T));
    out.close();
}

template void File::WriteBinaryFile(const long long *buf, int size, const std::string &file_name);
template void File::WriteBinaryFile(const int *buf, int size, const std::string &file_name);
template void File::WriteBinaryFile(const unsigned char *buf, int size, const std::string &file_name);
template void File::WriteBinaryFile(const float *buf, int size, const std::string &file_name);

template<typename T>
T get_json_value(const json &jsonData, const std::string &key, const T &defaultValue)
{
    try
    {
        if (!jsonData.contains(key))
        {
            return defaultValue;
        }

        if constexpr (std::is_same<T, std::string>::value)
        {

            if (jsonData[key].is_string())
            {
                return jsonData[key].get<T>();
            }

            else if (jsonData[key].is_array())
            {
                std::string texts = "";

                for (const auto &item: jsonData[key])
                {
                    if (item.is_string())
                    {

                        texts += item.get<std::string>();
                    }
                    else if (item.is_object())
                    {

                        if (item.contains("type") && item.contains("text"))
                        {
                            std::string type = item["type"].get<std::string>();
                            if (type == "text")
                            {
                                if (!texts.empty())
                                {
                                    texts += " ";
                                }
                                texts += item["text"].get<std::string>();
                            }
                        }
                        else
                        {

                            for (auto it = item.begin(); it != item.end(); ++it)
                            {
                                if (it.value().is_string())
                                {
                                    if (!texts.empty())
                                    {
                                        texts += " ";
                                    }
                                    texts += it.value().get<std::string>();
                                }
                            }
                        }
                    }
                }

                return texts;
            }

            else if (jsonData[key].is_null())
            {
                return defaultValue;
            }

            else
            {
                My_Log{My_Log::Level::kWarning} << "[get_json_value] Unexpected type for key: " << key << std::endl;
                return defaultValue;
            }
        }
        else
        {

            return jsonData[key].get<T>();
        }
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kError} << "[get_json_value] Exception for key: " << key << ", error: " << e.what() << std::endl;
        throw std::runtime_error("getting json value for key: " + key + " ," + e.what());
    }

    return defaultValue;
}

template std::string get_json_value(const json &jsonData, const std::string &key, const std::string &defaultValue);
template int get_json_value(const json &jsonData, const std::string &key, const int &defaultValue);
template double get_json_value(const json &jsonData, const std::string &key, const double &defaultValue);
template bool get_json_value(const json &jsonData, const std::string &key, const bool &defaultValue);
