#pragma once

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <mutex>
#include <string>
#include <system_error>
#include <vector>

namespace qnn_ckpt
{
namespace fs = std::filesystem;

struct Config
{
    std::atomic<bool> enabled{true};
    std::atomic<uint64_t> max_bytes{3ull << 30};
    std::atomic<int> max_checkpoints{6};
    std::atomic<bool> tail_fallback{true};
    std::string dir;
};

inline Config &GlobalConfig()
{
    static Config c;
    return c;
}

struct Registry
{
    std::mutex mu;
    std::map<std::string, int> active;
};

inline Registry &GlobalRegistry()
{
    static Registry r;
    return r;
}

class Hasher
{
public:
    void Update(const char *p, size_t n)
    {
        for (size_t i = 0; i < n; ++i)
        {
            const uint64_t c = static_cast<uint8_t>(p[i]);
            a_ = (a_ ^ c) * 1099511628211ull;
            b_ = (b_ + c + 1) * 0x9E3779B97F4A7C15ull;
            b_ ^= b_ >> 29;
        }
        len_ += n;
    }

    std::string Name() const
    {
        char buf[48];
        std::snprintf(buf, sizeof(buf), "%016llx%016llx", static_cast<unsigned long long>(a_),
                      static_cast<unsigned long long>(b_));
        return std::string(buf) + "-" + std::to_string(len_);
    }

private:
    uint64_t a_{1469598103934665603ull};
    uint64_t b_{0x243F6A8885A308D3ull};
    uint64_t len_{0};
};

inline std::string HashString(const std::string &s)
{
    Hasher h;
    h.Update(s.data(), s.size());
    return h.Name();
}

struct Entry
{
    std::string name;
    std::string dir;
    uint32_t n_past{0};
    uint32_t n_tokens{0};
    uint64_t kv_bytes{0};
    uint64_t kv_sum{0};
    uint64_t total_bytes{0};
    uint64_t tick{0};
    bool head{false};
    uint32_t n_gen{0};
};

inline const char *kKvFile = "kv-cache.primary.qnn-htp";
inline const char *kDialogFile = "dialog.json";
inline const char *kManifestFile = "manifest.txt";

inline uint32_t ReadDialogCounter(const fs::path &dir, const std::string &key)
{
    std::ifstream in(dir / kDialogFile, std::ios::binary);
    if (!in)
    {
        return 0;
    }
    const std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    const size_t at = text.find("\"" + key + "\"");
    if (at == std::string::npos)
    {
        return 0;
    }
    const size_t colon = text.find(':', at);
    if (colon == std::string::npos)
    {
        return 0;
    }
    try
    {
        return static_cast<uint32_t>(std::stoul(text.substr(colon + 1)));
    }
    catch (...)
    {
        return 0;
    }
}

inline bool SampledSum(const fs::path &p, uint64_t &size, uint64_t &sum)
{
    std::error_code ec;
    size = fs::file_size(p, ec);
    if (ec)
    {
        return false;
    }
    std::ifstream in(p, std::ios::binary);
    if (!in)
    {
        return false;
    }
    constexpr uint64_t kEdge = 65536;
    constexpr uint64_t kBlock = 4096;
    constexpr uint64_t kBlocks = 14;
    std::vector<char> buf(kEdge);
    uint64_t h = 1469598103934665603ull ^ size;
    auto feed = [&](uint64_t off, uint64_t n)
    {
        if (off >= size)
        {
            return true;
        }
        n = std::min<uint64_t>(n, size - off);
        in.clear();
        in.seekg(static_cast<std::streamoff>(off));
        in.read(buf.data(), static_cast<std::streamsize>(n));
        if (static_cast<uint64_t>(in.gcount()) != n)
        {
            return false;
        }
        for (uint64_t i = 0; i < n; ++i)
        {
            h = (h ^ static_cast<uint8_t>(buf[i])) * 1099511628211ull;
        }
        return true;
    };
    if (!feed(0, kEdge) || !feed(size > kEdge ? size - kEdge : 0, kEdge))
    {
        return false;
    }
    for (uint64_t i = 1; i <= kBlocks; ++i)
    {
        if (!feed(size / (kBlocks + 1) * i, kBlock))
        {
            return false;
        }
    }
    sum = h;
    return true;
}

inline uint64_t DirBytes(const fs::path &dir)
{
    uint64_t total = 0;
    std::error_code ec;
    for (fs::directory_iterator it(dir, ec), end; !ec && it != end; it.increment(ec))
    {
        if (it->path().filename() == kManifestFile || !it->is_regular_file(ec))
        {
            continue;
        }
        total += it->file_size(ec);
    }
    return total;
}

inline bool IsContextDirName(const std::string &name)
{
    const size_t dash = name.find('-');
    if (dash != 32 || dash + 1 >= name.size())
    {
        return false;
    }
    for (size_t i = 0; i < dash; ++i)
    {
        const char c = name[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
        {
            return false;
        }
    }
    for (size_t i = dash + 1; i < name.size(); ++i)
    {
        if (name[i] < '0' || name[i] > '9')
        {
            return false;
        }
    }
    return true;
}

inline void SweepRoot(const fs::path &root, const std::map<std::string, int> &active)
{
    struct Dir
    {
        fs::path path;
        fs::file_time_type mtime;
        uint64_t bytes{0};
        size_t entries{0};
    };
    std::error_code ec;
    std::vector<Dir> idle;
    std::vector<fs::path> stale;
    uint64_t total_bytes = 0;
    size_t total_entries = 0;
    for (fs::directory_iterator it(root, ec), end; !ec && it != end; it.increment(ec))
    {
        std::error_code ec2;
        if (!it->is_directory(ec2) || !IsContextDirName(it->path().filename().string()))
        {
            continue;
        }
        const bool live = active.count(it->path().filename().string()) > 0;
        Dir d;
        d.path = it->path();
        d.mtime = fs::last_write_time(d.path, ec2);
        for (fs::directory_iterator sub(d.path, ec2), send; !ec2 && sub != send; sub.increment(ec2))
        {
            std::error_code ec3;
            if (!sub->is_directory(ec3))
            {
                continue;
            }
            if (sub->path().filename().string().rfind(".tmp-", 0) == 0)
            {
                if (!live)
                {
                    stale.push_back(sub->path());
                }
                continue;
            }
            ++d.entries;
            d.bytes += DirBytes(sub->path());
        }
        total_bytes += d.bytes;
        total_entries += d.entries;
        if (!live)
        {
            idle.push_back(std::move(d));
        }
    }
    for (const auto &p: stale)
    {
        fs::remove_all(p, ec);
    }
    std::sort(idle.begin(), idle.end(), [](const Dir &a, const Dir &b) { return a.mtime < b.mtime; });
    const uint64_t max_bytes = GlobalConfig().max_bytes.load();
    const size_t max_count = static_cast<size_t>(std::max(1, GlobalConfig().max_checkpoints.load()));
    for (const auto &d: idle)
    {
        if (d.entries > 0 && total_bytes <= max_bytes && total_entries <= max_count)
        {
            continue;
        }
        total_bytes -= d.bytes;
        total_entries -= d.entries;
        fs::remove_all(d.path, ec);
    }
}

inline bool WriteManifest(const fs::path &dir, const Entry &e, const std::string &ctx_key)
{
    const fs::path tmp = dir / (std::string(kManifestFile) + ".tmp");
    {
        std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
        if (!out)
        {
            return false;
        }
        out << "v=1\nctx=" << ctx_key << "\nn_past=" << e.n_past << "\nn_tokens=" << e.n_tokens
            << "\nkv_bytes=" << e.kv_bytes << "\nkv_sum=" << e.kv_sum << "\ntotal_bytes=" << e.total_bytes
            << "\nhead=" << (e.head ? 1 : 0) << "\nn_gen=" << e.n_gen << "\n";
        out.flush();
        if (!out)
        {
            return false;
        }
    }
    std::error_code ec;
    fs::rename(tmp, dir / kManifestFile, ec);
    return !ec;
}

inline bool ReadManifest(const fs::path &dir, Entry &e, std::string &ctx_key)
{
    std::ifstream in(dir / kManifestFile, std::ios::binary);
    if (!in)
    {
        return false;
    }
    std::map<std::string, std::string> kv;
    std::string line;
    while (std::getline(in, line))
    {
        const size_t eq = line.find('=');
        if (eq != std::string::npos)
        {
            kv[line.substr(0, eq)] = line.substr(eq + 1);
        }
    }
    try
    {
        if (kv.at("v") != "1")
        {
            return false;
        }
        ctx_key = kv.at("ctx");
        e.n_past = static_cast<uint32_t>(std::stoul(kv.at("n_past")));
        e.n_tokens = static_cast<uint32_t>(std::stoul(kv.at("n_tokens")));
        e.kv_bytes = std::stoull(kv.at("kv_bytes"));
        e.kv_sum = std::stoull(kv.at("kv_sum"));
        e.total_bytes = std::stoull(kv.at("total_bytes"));
        e.head = kv.at("head") == "1";
        e.n_gen = static_cast<uint32_t>(std::stoul(kv.at("n_gen")));
    }
    catch (...)
    {
        return false;
    }
    e.dir = dir.string();
    return true;
}

inline bool Validate(const Entry &e, std::string &why)
{
    const fs::path d = e.dir;
    std::error_code ec;
    if (!fs::is_directory(d, ec))
    {
        why = "dir_missing";
        return false;
    }
    if (!fs::exists(d / kDialogFile, ec) || !fs::exists(d / kKvFile, ec))
    {
        why = "file_missing";
        return false;
    }
    uint64_t size = 0;
    uint64_t sum = 0;
    if (!SampledSum(d / kKvFile, size, sum))
    {
        why = "kv_unreadable";
        return false;
    }
    if (size != e.kv_bytes)
    {
        why = "kv_size";
        return false;
    }
    if (sum != e.kv_sum)
    {
        why = "kv_sum";
        return false;
    }
    if (DirBytes(d) != e.total_bytes)
    {
        why = "total_bytes";
        return false;
    }
    return true;
}

class Store
{
public:
    Store() = default;

    Store(const Store &) = delete;

    Store &operator=(const Store &) = delete;

    ~Store() { Close(); }

    void Open(const fs::path &root, const std::string &ctx_name)
    {
        Close();
        ctx_dir_ = root / ctx_name;
        ctx_key_ = ctx_name;
        std::error_code ec;
        bool shared = false;
        {
            std::lock_guard<std::mutex> lock(GlobalRegistry().mu);
            shared = ++GlobalRegistry().active[ctx_name] > 1;
            active_name_ = ctx_name;
            fs::create_directories(ctx_dir_, ec);
            SweepRoot(root, GlobalRegistry().active);
        }
        std::vector<std::pair<fs::file_time_type, Entry>> found;
        std::vector<fs::path> stale;
        for (fs::directory_iterator it(ctx_dir_, ec), end; !ec && it != end; it.increment(ec))
        {
            std::error_code ec2;
            if (!it->is_directory(ec2))
            {
                continue;
            }
            const fs::path p = it->path();
            const std::string name = p.filename().string();
            Entry e;
            std::string key;
            if (name.rfind(".tmp-", 0) == 0)
            {
                if (!shared)
                {
                    stale.push_back(p);
                }
                continue;
            }
            if (!ReadManifest(p, e, key) || key != ctx_key_)
            {
                stale.push_back(p);
                continue;
            }
            e.name = name;
            found.emplace_back(fs::last_write_time(p / kManifestFile, ec2), e);
        }
        for (const auto &p: stale)
        {
            fs::remove_all(p, ec);
        }
        std::sort(found.begin(), found.end(),
                  [](const auto &a, const auto &b) { return a.first < b.first; });
        for (auto &f: found)
        {
            f.second.tick = ++tick_;
            entries_[f.second.name] = f.second;
        }
        Evict(std::string());
        fs::last_write_time(ctx_dir_, fs::file_time_type::clock::now(), ec);
    }

    void Close()
    {
        entries_.clear();
        if (active_name_.empty())
        {
            return;
        }
        std::lock_guard<std::mutex> lock(GlobalRegistry().mu);
        auto it = GlobalRegistry().active.find(active_name_);
        if (it != GlobalRegistry().active.end() && --it->second <= 0)
        {
            GlobalRegistry().active.erase(it);
        }
        active_name_.clear();
    }

    bool Lookup(const std::string &name, Entry &out, std::string &why)
    {
        why.clear();
        auto it = entries_.find(name);
        if (it == entries_.end())
        {
            return false;
        }
        if (!Validate(it->second, why))
        {
            Remove(name);
            return false;
        }
        out = it->second;
        return true;
    }

    void Touch(const std::string &name)
    {
        auto it = entries_.find(name);
        if (it == entries_.end())
        {
            return;
        }
        it->second.tick = ++tick_;
        std::error_code ec;
        const auto now = fs::file_time_type::clock::now();
        fs::last_write_time(fs::path(it->second.dir) / kManifestFile, now, ec);
        fs::last_write_time(ctx_dir_, now, ec);
    }

    fs::path NewTempDir()
    {
        const auto now = std::chrono::steady_clock::now().time_since_epoch().count();
        return ctx_dir_ / (".tmp-" + std::to_string(reinterpret_cast<uintptr_t>(this)) + "-" +
                           std::to_string(now) + "-" + std::to_string(++seq_));
    }

    bool Commit(const fs::path &tmp, const std::string &name, uint32_t n_past, uint32_t n_tokens, bool head,
                Entry &out, std::string &why)
    {
        std::error_code ec;
        Entry e;
        e.name = name;
        e.n_past = n_past;
        e.n_tokens = n_tokens;
        e.head = head;
        e.n_gen = ReadDialogCounter(tmp, "n-generated");
        uint64_t size = 0;
        uint64_t sum = 0;
        if (!fs::exists(tmp / kDialogFile, ec) || !SampledSum(tmp / kKvFile, size, sum) || size == 0)
        {
            why = "save_incomplete";
            fs::remove_all(tmp, ec);
            return false;
        }
        e.kv_bytes = size;
        e.kv_sum = sum;
        e.total_bytes = DirBytes(tmp);
        if (e.total_bytes > GlobalConfig().max_bytes.load())
        {
            why = "over_budget";
            fs::remove_all(tmp, ec);
            return false;
        }
        if (!WriteManifest(tmp, e, ctx_key_))
        {
            why = "manifest";
            fs::remove_all(tmp, ec);
            return false;
        }
        const fs::path final_dir = ctx_dir_ / name;
        Remove(name);
        fs::rename(tmp, final_dir, ec);
        if (ec)
        {
            why = "rename";
            fs::remove_all(tmp, ec);
            return false;
        }
        e.dir = final_dir.string();
        e.tick = ++tick_;
        entries_[name] = e;
        out = e;
        Evict(name);
        return true;
    }

    void Remove(const std::string &name)
    {
        std::error_code ec;
        entries_.erase(name);
        fs::remove_all(ctx_dir_ / name, ec);
    }

    size_t Count() const { return entries_.size(); }

    uint64_t Bytes() const
    {
        uint64_t total = 0;
        for (const auto &kv: entries_)
        {
            total += kv.second.total_bytes;
        }
        return total;
    }

private:
    void Evict(const std::string &keep)
    {
        const size_t max_count = static_cast<size_t>(std::max(1, GlobalConfig().max_checkpoints.load()));
        const uint64_t max_bytes = GlobalConfig().max_bytes.load();
        while (entries_.size() > max_count || Bytes() > max_bytes)
        {
            size_t heads = 0;
            for (const auto &kv: entries_)
            {
                heads += kv.second.head ? 1 : 0;
            }
            const bool evict_head = heads > std::max<size_t>(1, max_count / 2);
            const Entry *victim = nullptr;
            const Entry *head_victim = nullptr;
            for (const auto &kv: entries_)
            {
                if (kv.first == keep)
                {
                    continue;
                }
                const Entry *&slot = kv.second.head ? head_victim : victim;
                if (!slot || kv.second.tick < slot->tick)
                {
                    slot = &kv.second;
                }
            }
            if (evict_head || !victim)
            {
                victim = head_victim ? head_victim : victim;
            }
            if (!victim)
            {
                return;
            }
            const std::string name = victim->name;
            Remove(name);
        }
    }

    fs::path ctx_dir_;
    std::string ctx_key_;
    std::string active_name_;
    std::map<std::string, Entry> entries_;
    uint64_t tick_{0};
    uint64_t seq_{0};
};

inline constexpr const char *kMarker = "<|im_start|>";
inline constexpr const char *kMessageEnd = "<|im_end|>\n";

inline std::vector<size_t> FindBoundaries(const std::string &text)
{
    std::vector<size_t> cuts;
    const std::string marker = kMarker;
    const std::string end = kMessageEnd;
    for (size_t p = text.find(marker, 1); p != std::string::npos; p = text.find(marker, p + 1))
    {
        if (p >= end.size() && text.compare(p - end.size(), end.size(), end) == 0)
        {
            cuts.push_back(p);
        }
    }
    return cuts;
}

inline int HeadIndex(const std::string &text, const std::vector<size_t> &cuts)
{
    const std::string system_head = std::string(kMarker) + "system\n";
    if (text.compare(0, system_head.size(), system_head) != 0)
    {
        return -1;
    }
    for (size_t i = 0; i < cuts.size(); ++i)
    {
        if (text.compare(cuts[i], system_head.size(), system_head) != 0)
        {
            return static_cast<int>(i);
        }
    }
    return -1;
}

inline std::vector<std::string> PrefixNames(const std::string &text, const std::vector<size_t> &cuts)
{
    std::vector<std::string> names;
    names.reserve(cuts.size());
    Hasher h;
    size_t pos = 0;
    for (size_t c: cuts)
    {
        h.Update(text.data() + pos, c - pos);
        pos = c;
        names.push_back(h.Name());
    }
    return names;
}
}
