//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include <iostream>
#include <windows.h>
#include <fstream>
#include <vector>

#pragma comment(lib, "Crypt32.lib")
using namespace std;

int main(int argc, char **argv)
{
    if (argc != 3)
    {
        cout << "please input the file path that needs to be decode and the output path";
        return 1;
    }

    ifstream in(argv[1], std::ios::binary);
    if (!in.good())
    {
        std::cout << "open encoded_message.txt failed\n";
        return -1;
    }

    in.seekg(0, std::ios::end);
    std::vector<CHAR> encoded_buf(in.tellg());
    in.seekg(0, std::ios::beg);
    if (!in.read(reinterpret_cast<char *>(encoded_buf.data()), encoded_buf.size()))
    {
        std::cout << "read form file failed\n";
        return -1;
    }

    DWORD dwDecodeFlag{CRYPT_STRING_BASE64_ANY};
    DWORD dwDecodeLen;
    DWORD dwFlag;

    if (!CryptStringToBinaryA(encoded_buf.data(),
                              encoded_buf.size(),
                              dwDecodeFlag,
                              nullptr,
                              &dwDecodeLen,
                              nullptr,
                              &dwFlag))
    {
        std::cout << "decode to binrary failed before alloc: " << GetLastError() << "\n";
        return -1;
    }

    ERROR_SUCCESS;

    auto buf = new uint8_t[dwDecodeLen]{};
    if (!CryptStringToBinaryA(encoded_buf.data(),
                              encoded_buf.size(),
                              dwDecodeFlag,
                              buf,
                              &dwDecodeLen,
                              nullptr,
                              &dwFlag))
    {
        std::cout << "decode to binrary failed after alloc: " << GetLastError() << "\n";
        delete[] buf;
        return -1;
    }

    std::cout << "decode success, decode size:" << dwDecodeLen << " decode type: " << dwFlag << "\n";
    ofstream out(argv[2], std::ios::binary);
    out.write(reinterpret_cast<char *>(buf), dwDecodeLen);
    delete[] buf;
    return 0;
}