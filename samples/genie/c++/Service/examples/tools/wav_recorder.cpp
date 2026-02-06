//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

// record_wav.cpp
#include <windows.h>
#include <mmsystem.h>
#include <fstream>
#include <vector>
#include <iostream>
#include <thread>
#include <mmdeviceapi.h>
#include <endpointvolume.h>

#pragma comment(lib, "winmm.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "ole32.lib")

std::vector<uint8_t> total_data;
std::atomic<bool> need_buf{false};
std::atomic<bool> working{false};

class SoundBoosterHelper
{
public:
    static bool BoostVoiceInput()
    {
        HRESULT hr = CoInitializeEx(NULL, COINIT_MULTITHREADED);
        if (FAILED(hr))
        {
            std::cerr << "initiate com failed\n";
            return false;
        }

        IMMDeviceEnumerator *pEnum = nullptr;
        hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), NULL, CLSCTX_ALL,
                              __uuidof(IMMDeviceEnumerator), (void **) &pEnum);
        if (FAILED(hr))
        {
            std::cerr << "create com instance failed\n";
            CoUninitialize();
            return false;
        }

        IMMDevice *pDevice = nullptr;
        hr = pEnum->GetDefaultAudioEndpoint(eCapture, eConsole, &pDevice); // capture endpoint
        if (FAILED(hr))
        {
            std::cerr << "get default audio endpoint failed\n";
            pEnum->Release();
            CoUninitialize();
            return false;
        }

        IAudioEndpointVolume *pEndpointVol = nullptr;
        hr = pDevice->Activate(__uuidof(IAudioEndpointVolume), CLSCTX_ALL, NULL, (void **) &pEndpointVol);
        if (FAILED(hr))
        {
            std::cerr << "get IAudioEndpointVolume pointer failed\n";
            pDevice->Release();
            pEnum->Release();
            CoUninitialize();
            return false;
        }

        hr = pEndpointVol->SetMasterVolumeLevelScalar(100 / 100.0f, nullptr);
        if (FAILED(hr))
        {
            std::cout << "set volume to max failed: " << hr << "\n";
        }

        float level_scalar = 0.0f;
        hr = pEndpointVol->GetMasterVolumeLevelScalar(&level_scalar); // 0.0 .. 1.0
        if (SUCCEEDED(hr))
        {
            float percent = level_scalar * 100.0f;
            std::cout << "input volume: " << percent << "%\n";
        }

        pEndpointVol->Release();
        pDevice->Release();
        pEnum->Release();
        CoUninitialize();
        return true;
    }

    static void apply_gain_int16_safe(int16_t *s, int n, float gain)
    {
        const float scale = 32767.0f; // map float [-1,1] -> int16
        for (int i = 0; i < n; ++i)
        {
            // convert to float in [-1,1]
            float x = s[i] / scale;
            // apply gain
            float y = x * gain;
            // hard clamp to avoid overflow
            if (y > 0.999969f)
                y = 0.999969f;
            if (y < -0.999969f)
                y = -0.999969f;
            // convert back with rounding
            int32_t out = (int32_t) lrintf(y * scale);
            s[i] = (int16_t) clamp_i32(out);
        }
    }

private:
    static inline int32_t clamp_i32(int32_t v)
    {
        if (v > 32767)
            return 32767;
        if (v < -32768)
            return -32768;
        return v;
    }
};

void ControlRecord(HWAVEIN hWaveIn)
{
    std::cout << "Please long press the [Space] key for Recording...\n";
    while (true)
    {
        if (!(GetKeyState(VK_SPACE) & 0x8000))
        {
            Sleep(10);
            continue;
        }

        MMRESULT mmRes;
        if (MMSYSERR_NOERROR != (mmRes = waveInStart(hWaveIn)))
        {
            std::cerr << "starts waveform-audio recording failed: " << mmRes << "\n";
            break;
        }
        DWORD t0 = GetTickCount();
        while (GetKeyState(VK_SPACE) & 0x8000)
            Sleep(10);

        DWORD t1 = GetTickCount();
        waveInReset(hWaveIn);
        if (t1 - t0 >= 1000 /*1ms*/)
        {
            working = false;
            break;
        }
        else
        {
            total_data.clear();
            std::cout << "short press!, please retry\n";
        }
    }
    FlushConsoleInputBuffer(GetStdHandle(STD_INPUT_HANDLE));
}

class WAV
{
public:
    WAV(char *buf, long size) : buf_{buf}
    {
        total_size_ = offsetof(WAV_HEADER, WAV_HEADER::padding) + size;
        whdr_ = reinterpret_cast<WAV_HEADER *>(new uint8_t[total_size_]);
        new(whdr_) WAV_HEADER;
        whdr_->overall_size += size;
        whdr_->data_size = size;
    }

    ~WAV()
    {
        if (whdr_)
            delete[] reinterpret_cast<uint8_t *>(whdr_);
    }

    void Write(const std::string &path)
    {
        memcpy(whdr_->padding, buf_, whdr_->data_size);
        std::ofstream out(path, std::ios::binary);
        out.write(reinterpret_cast<char *>(whdr_), total_size_);
    }

    struct WAV_HEADER
    {
        char riff[4]{'R', 'I', 'F', 'F'};
        uint32_t overall_size{36};
        char wave[4]{'W', 'A', 'V', 'E'};
        char fmt_chunk_marker[4]{'f', 'm', 't', ' '};
        uint32_t length_of_fmt{16};
        uint16_t format_type{WAVE_FORMAT_PCM};
        uint16_t channels{kChannels};
        uint32_t sample_rate{kSmplePreRate};
        uint32_t avgbyterate{kAvgByteRate};
        uint16_t block_align{kBlockAlign};
        uint16_t bits_per_sample{kBitsPerSample};
        char data_chunk_header[4]{'d', 'a', 't', 'a'};
        uint32_t data_size{};
        uint8_t padding[0];
    };

    static inline uint16_t kBitsPerSample{16};
    static inline uint16_t kChannels{2};
    static inline uint32_t kSmplePreRate{44100};
    static inline uint16_t kBlockAlign = kChannels * kBitsPerSample / 8; // {} avoid narrow;
    static inline uint32_t kAvgByteRate = kSmplePreRate * kBlockAlign;

private:
    char *buf_;
    long total_size_;
    WAV_HEADER *whdr_;
};

// global or context

void
CALLBACK waveInProc(HWAVEIN hWaveIn, UINT uMsg, DWORD_PTR dwInstance, DWORD_PTR /*dwParam1*/, DWORD_PTR /*ddwParam2*/)
{
    WAVEHDR *hdr{reinterpret_cast<WAVEHDR *>(dwInstance)};
    int samples;
    switch (uMsg)
    {
        case WIM_CLOSE:
            std::cout << "WIM_CLOSE is sent\n";
            break;
        case WIM_DATA:
            samples = hdr->dwBytesRecorded / (WAV::kBitsPerSample / 8);
            SoundBoosterHelper::apply_gain_int16_safe((int16_t *) hdr->lpData, samples, 3.0f);
            total_data.insert(total_data.end(),
                              reinterpret_cast<uint8_t *>(hdr->lpData),
                              reinterpret_cast<uint8_t *>(hdr->lpData) + hdr->dwBytesRecorded);
            need_buf = true;
            break;
        case WIM_OPEN:
            std::cout << "WIM_OPEN is sent\n";
            break;
    }
}

int main(int argc, char **argv)
{
    if (argc != 2)
    {
        std::cout << "please input the output file path\n";
        return 1;
    }

    SoundBoosterHelper::BoostVoiceInput();
    WAVEFORMATEX wf{
            WAVE_FORMAT_PCM,
            WAV::kChannels,
            WAV::kSmplePreRate,
            WAV::kAvgByteRate,
            WAV::kBlockAlign,
            WAV::kBitsPerSample,
            0};

    const DWORD dataBytes = wf.nAvgBytesPerSec; // 1s as buf
    std::vector<BYTE> buffer(dataBytes);

    HWAVEIN hWaveIn{nullptr};
    MMRESULT mmRes;
    WAVEHDR header{reinterpret_cast<LPSTR>(buffer.data()), dataBytes};
    WAV *wav{};
    std::thread *t;

    if (MMSYSERR_NOERROR != (mmRes = waveInOpen(&hWaveIn,
                                                WAVE_MAPPER,
                                                &wf,
                                                (DWORD_PTR) &waveInProc,
                                                (DWORD_PTR) &header,
                                                CALLBACK_FUNCTION | WAVE_MAPPED_DEFAULT_COMMUNICATION_DEVICE)))
    {
        std::cerr << "open waveform-audio handle failed: " << mmRes << "\n";
        goto ahead;
    }

    if (MMSYSERR_NOERROR != (mmRes = waveInPrepareHeader(hWaveIn, &header, sizeof(header))))
    {
        std::cerr << "prepare waveform-audio input failed: " << mmRes << "\n";
        goto ahead;
    }

    working = true;
    need_buf = true;
    t = new std::thread{[&]()
                        {
                            while (working)
                            {
                                if (!need_buf)
                                {
                                    goto next;
                                }

                                if (MMSYSERR_NOERROR != (mmRes = waveInAddBuffer(hWaveIn, &header, sizeof(header))))
                                {
                                    std::cerr << "send buf to waveform-audio input device failed: " << mmRes << "\n";
                                    break;
                                }

                                need_buf = false;
                                next:
                                Sleep(200);
                            }
                        }};

    ControlRecord(hWaveIn);
    t->join();
    delete t;

    wav = new WAV(reinterpret_cast<char *>(total_data.data()), total_data.size());
    wav->Write(argv[1]);
    delete wav;
    std::cout << "Saved wav succcessfully\n";

    ahead:
    if (header.lpData)
        waveInUnprepareHeader(hWaveIn, &header, sizeof(header));

    if (hWaveIn)
        waveInClose(hWaveIn);
    return 0;
}
