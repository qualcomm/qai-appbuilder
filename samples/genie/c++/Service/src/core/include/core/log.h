#ifndef GENIEAPICLIENT_SLN_LOG_H
#define GENIEAPICLIENT_SLN_LOG_H

#include <GenieLog.h>
#include <sstream>
#include <mutex>
#include <fstream>
#include <iomanip>
#include <filesystem>
#include <functional>

#if defined(_WIN32) || defined(__WIN32__) || defined(WIN32)

#include <direct.h>
#include <io.h>

#define RED "\033[31m"
#define GREEN "\033[32m"
#define YELLOW "\033[33m"
#define BLUE "\033[34m"
#define MAGENTA "\033[35m"
#define CYAN "\033[36m"
#define BOLD "\033[1m"
#define UNDERLINE "\033[4m"
#define ITALIC "\033[3m"
#define RESET "\033[0m"

#endif

#ifdef __ANDROID__
#include <android/log.h>
#include <unistd.h>

#define RED ""
#define GREEN ""
#define YELLOW ""
#define BLUE ""
#define MAGENTA ""
#define CYAN ""
#define BOLD ""
#define UNDERLINE ""
#define ITALIC ""
#define RESET ""
#endif


class My_Log
{
public:
    enum Level
    {
        kAlways,
        kError,
        kWarning,
        kInfo,
        kDebug,
        kVerbose,
    };

    static GenieLog_Level_t get_genie_log_level()
    {
        switch (LoggerHelper::lev_)
        {
            case My_Log::Level::kError:
                return GENIE_LOG_LEVEL_ERROR;
            case My_Log::Level::kWarning:
                return GENIE_LOG_LEVEL_WARN;
            case My_Log::Level::kInfo:
                return GENIE_LOG_LEVEL_INFO;
            default:
                return GENIE_LOG_LEVEL_VERBOSE;
        }
    }

    static void Init(Level lev, std::string &&log_path)
    {
        LoggerHelper::lev_ = (lev > Level::kVerbose || lev <= Level::kAlways) ? Level::kWarning : lev;
        if (log_path.empty())
        {
            func_ = &My_Log::LogConsole;
            return;
        }

        LoggerHelper::kFile_.log_path_ = std::move(log_path);
        LoggerHelper::kFile_.tmp_path_ =
                std::filesystem::path{LoggerHelper::kFile_.log_path_}.parent_path().append("tmp.log").generic_string();
        LoggerHelper::kFile_.out_.close();
        LoggerHelper::kFile_.out_.open(LoggerHelper::kFile_.log_path_, std::ios::app | std::ios::binary);

        if (!LoggerHelper::kFile_.out_.is_open())
        {
            func_ = &My_Log::LogConsole;
            My_Log{My_Log::Level::kAlways} << "create or open file failed:  " << errno << std::endl;
            return;
        }

        func_ = &My_Log::LogFile;
    }

    static void ShowStatus()
    {
        My_Log{My_Log::Level::kWarning} << "log level setting: " << LoggerHelper::lev_ << std::endl;
        if (!LoggerHelper::kFile_.log_path_.empty())
        {
            My_Log{My_Log::Level::kWarning} << "log path: " << LoggerHelper::kFile_.log_path_ << std::endl;
        }
        if (!LoggerHelper::kFile_.tmp_path_.empty())
        {
            My_Log{My_Log::Level::kWarning} << "tmp path: " << LoggerHelper::kFile_.tmp_path_ << std::endl;
        }

        char buffer[1024];
        if (getcwd(buffer, sizeof(buffer)))
        {
            My_Log{My_Log::Level::kWarning} << "current work dir: " << buffer << std::endl;
        }
    }

    // assume not fragement
    My_Log(char *msg, Level lev = kWarning) : My_Log{lev}
    {
        Log(msg, LoggerHelper::kConsoleBufferLen - 1);
    }

    // output once. like My_Log(const std::string& message);
    My_Log(const std::string &msg, Level lev = kWarning) : My_Log{lev}
    {
        Log(msg.c_str(), msg.size());
    }

    explicit My_Log(Level lev = kWarning) : lev_{lev}
    {}

    template<typename T>
    My_Log &operator<<(const T &value)
    {
        os_ << value;
        return *this;
    }

    My_Log &operator<<(std::ostream &(*func)(std::ostream &))
    {
        func(os_);
        // std::endl(os_);

        if (func == static_cast<std::ostream &(*)(std::ostream &)>(std::endl) ||
            func == static_cast<std::ostream &(*)(std::ostream &)>(std::flush))
        {
            Log(os_.str().c_str(), os_.str().size());
            os_.str("");
        }
        return *this;
    }

    ~My_Log()
    {
        if (os_.tellp() != std::streampos{0})
        {
            Log(os_.str().c_str(), os_.str().size());
        }
    }

    My_Log &set_tag(const char *const tag)
    {
        tag_ = const_cast<char *>(tag);
        return *this;
    }

    My_Log &original(bool output = true)
    {
        if (output)
        {
            get_log_title_ = []()
            { return ""; };
        }
        return *this;
    }

    static std::string GetTimeString()
    {
        /*
        std::time_t t = std::time(nullptr);
        std::tm tm = *std::localtime(&t);
        std::ostringstream oss;
        oss << std::put_time(&tm, "%Y-%m-%d %H:%M:%S");
        return oss.str();
        */

        auto now = std::chrono::system_clock::now();
        std::time_t t = std::chrono::system_clock::to_time_t(now);
        auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()) % 1000;
        std::tm tm = *std::localtime(&t);
        std::ostringstream oss;
        oss << std::put_time(&tm, "%Y-%m-%d %H:%M:%S");
        oss << '.' << std::setfill('0') << std::setw(3) << milliseconds.count();

        return oss.str();
    }

private:
    class LoggerHelper
    {
    public:
        static inline struct Console
        {
            const uint16_t kConsoleBufferLen;
            const char *const kDefaultLogTag;

            constexpr Console() noexcept
                    : kConsoleBufferLen(1018),
                      kDefaultLogTag("genieapiservice_log")
            {}
        } kConsole_;

        static inline struct File
        {
            std::string log_path_;
            std::ofstream out_;
            const uint32_t kNewOffset_;
            const uint32_t kMaxSize_;
            std::string tmp_path_;

            File() noexcept
                    : kNewOffset_(4 * 1024 * 1024),
                      kMaxSize_{6 * 1024 * 1024}
            {}

            static void RotateFile(const std::string &from, const std::string &dst, uint32_t offset)
            {
                std::ifstream in(from, std::ios::binary);
                std::ofstream out(dst, std::ios::binary | std::ios::trunc);
                in.seekg(offset, std::ios::beg);
                out << in.rdbuf();
                in.close();
                out.close();
            }

        } kFile_;

        static inline Level lev_;
        static inline uint16_t const kConsoleBufferLen{1018}; // 1024 include "\n"

        static const char *get_level_str(Level lev)
        {
            switch (lev)
            {
                case kVerbose:
                    return " [V] ";
                case kDebug:
                    return " [D] ";
                case kWarning:
                    return " [W] ";
                case kInfo:
                    return " [I] ";
                case kError:
                    return " [E] ";
                case kAlways:
                    return " [A] ";
                default:
                    return " [U] ";
            }
        }
    };

    std::ostringstream os_;
    Level lev_{Level::kWarning};
    char *tag_{const_cast<char *>(LoggerHelper::kConsole_.kDefaultLogTag)};

    static inline void (My_Log::*func_)(const char *msg, uint32_t len);

    void Log(const char *msg, uint32_t len)
    {
        if (lev_ > LoggerHelper::lev_ || len <= 0 || msg == nullptr)
        {
            return;
        }

        (this->*func_)(msg, len);
    }

    void LogConsole(const char *msg, uint32_t len)
    {
#ifdef __ANDROID__
#define OUT_PUT(...) \
        __android_log_print(ANDROID_LOG_DEBUG, tag_, "%.*s", ##__VA_ARGS__);
#else
#define OUT_PUT(...)  \
        printf("%.*s", ##__VA_ARGS__);
#endif
        auto title = get_log_title_();
        OUT_PUT(static_cast<int>(title.size()), title.c_str());

        char *cur = const_cast<char *>(msg);
        auto remaining = len;
        while (remaining > 0)
        {
            uint32_t chunk = (remaining < LoggerHelper::kConsoleBufferLen) ?
                             remaining : LoggerHelper::kConsoleBufferLen;
            OUT_PUT(chunk, cur);
            cur += chunk;
            remaining -= chunk;
        }
    }

    void LogFile(const char *msg, uint32_t len)
    {
        static std::mutex mtx;
        static int times;
        mtx.lock();

        LoggerHelper::kFile_.out_ << get_log_title_();
        LoggerHelper::kFile_.out_.write(msg, len);
        LoggerHelper::kFile_.out_.flush();
        if (times >= 20 && LoggerHelper::kFile_.out_.tellp() >= LoggerHelper::kFile_.kMaxSize_)
        {
            LoggerHelper::kFile_.out_.close();
            LoggerHelper::File::RotateFile(LoggerHelper::kFile_.log_path_,
                                           LoggerHelper::kFile_.tmp_path_,
                                           LoggerHelper::kFile_.kNewOffset_);

            LoggerHelper::File::RotateFile(LoggerHelper::kFile_.tmp_path_,
                                           LoggerHelper::kFile_.log_path_,
                                           0);
            LoggerHelper::kFile_.out_.open(LoggerHelper::kFile_.log_path_, std::ios::app | std::ios::binary);
            times = 0;
        }
        ++times;
        mtx.unlock();
    }

    std::function<std::string()> get_log_title_{[this]()
                                                {
                                                    return LoggerHelper::get_level_str(lev_);
                                                }};
};

#endif //GENIEAPICLIENT_SLN_LOG_H
