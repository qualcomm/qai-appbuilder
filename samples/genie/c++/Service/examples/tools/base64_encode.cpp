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
        cout << "please input the file path that needs to be encode and the output path";
        return 1;
    }

    ifstream in(argv[1], std::ios::binary);
    if (!in.good())
    {
        std::cout << "open file path failed\n";
        return 1;
    }
    in.seekg(0, std::ios::end);
    std::vector<CHAR> buf(in.tellg());
    in.seekg(0, std::ios::beg);
    if (!in.read(reinterpret_cast<char *>(buf.data()), buf.size()))
    {
        std::cout << "read form file failed\n";
        return 1;
    }

    DWORD dwByteNeeded;
    if (!CryptBinaryToStringA(reinterpret_cast<BYTE *>(buf.data()),
                              buf.size(),
                              CRYPT_STRING_BASE64 | CRYPT_STRING_NOCRLF,
                              nullptr,
                              &dwByteNeeded))
    {
        std::cout << "encode to binrary failed before alloc: " << GetLastError() << "\n";
        return 1;
    }

    auto out_buf = new uint8_t[dwByteNeeded]{};
    if (!CryptBinaryToStringA(reinterpret_cast<BYTE *>(buf.data()),
                              buf.size(),
                              CRYPT_STRING_BASE64 | CRYPT_STRING_NOCRLF,
                              reinterpret_cast<CHAR *>(out_buf),
                              &dwByteNeeded))
    {
        std::cout << "encode to binrary failed before alloc: " << GetLastError() << "\n";
        delete[] out_buf;
        return 1;
    }

    ofstream out(argv[2]);
    out.write(reinterpret_cast<char *>(out_buf), dwByteNeeded);
    delete[] out_buf;
    return 0;
}
