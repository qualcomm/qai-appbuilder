#pragma once

#include <string>
#include <vector>

#ifdef _WIN32
    #ifdef DLL_EXPORTS
        #define LIBAPPBUILDER_API __declspec(dllexport)
    #else
        #define LIBAPPBUILDER_API __declspec(dllimport)
    #endif
#else // _WIN32
    #define LIBAPPBUILDER_API
#endif


class LIBAPPBUILDER_API LoraAdaptor{
public:
    std::string m_graph_name;
    std::vector<std::string> m_bin_paths;

    LoraAdaptor(const std::string &graph_name, const std::vector<std::string> &bin_paths);

    ~LoraAdaptor();
};