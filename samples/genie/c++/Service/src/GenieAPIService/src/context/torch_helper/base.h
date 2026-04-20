#ifndef DEF_H
#define DEF_H

#include <vector>

struct FloatBufferView
{
    explicit FloatBufferView(const std::vector<uint8_t> &buffer)
    {
        pointer_ = reinterpret_cast<float *>(const_cast<std::vector<uint8_t> &>(buffer).data());
        size_ = buffer.size() / sizeof(float);
    }

    float *pointer_{};
    unsigned long size_{};
};

template<typename T>
struct Shape_1D
{
    int d0;
    std::vector<T> buf;
};

template<typename T>
struct Shape_2D
{
    int d0;
    int d1;
    std::vector<T> buf;
};

template<typename T>
struct Shape_2D_View
{
    int d0;
    int d1;
    T *buf;
    int size;
};

template<typename T>
struct Shape_3D
{
    int d0;
    int d1;
    int d2;
    std::vector<T> buf;
};

template<typename T>
struct Shape_4D
{
    int d0;
    int d1;
    int d2;
    int d3;
    std::vector<T> buf;
};

template<typename T>
struct Shape_4D_View
{
    int d0;
    int d1;
    int d2;
    int d3;
    T *buf;
    int size;
};

template<typename T>
struct Shape_5D
{
    int d0;
    int d1;
    int d2;
    int d3;
    int d4;
    std::vector<T> buf;
};

template<typename T>
struct Shape_6D
{
    int d0;
    int d1;
    int d2;
    int d3;
    int d4;
    int d5;
    std::vector<T> buf;

    void Shape();
};

template<typename T>
int Sum(const T &x, bool round_result = true)
{
    double sum = 0.0;
    const size_t n = static_cast<size_t>(x.d0);
    auto *data = x.buf.data();
    for (size_t i = 0; i < n; ++i)
    {
        sum += static_cast<double>(data[i]);
    }
//    if (round_result)
//        return static_cast<int>(std::lround(sum));
    return static_cast<int>(sum); // truncates toward zero
}

template<typename T>
Shape_1D<T> Arange(T start, T end, T step)
{
    Shape_1D<T> out;
    if (step == T(0) || (end > start && step < T(0)) || (end < start && step > T(0)))
    {
        out.d0 = 0;
        return out;
    }

    // Use a small epsilon to avoid floating-point accumulation issues
    const T eps = std::numeric_limits<T>::epsilon() * std::fabs(start + end + step) * T(10);

    std::vector<T> buf;
    T cur = start;

    if (step > T(0))
    {
        while (cur + eps < end)
        {
            buf.push_back(cur);
            cur += step;
        }
    }
    else
    {
        while (cur - eps > end)
        {
            buf.push_back(cur);
            cur += step;
        }
    }

    out.d0 = static_cast<int>(buf.size());
    out.buf = std::move(buf);
    return out;
}

// Bucketize: returns indices as Shape_1D<int>
// Semantics: right = true -> boundaries[i-1] < x <= boundaries[i]
template<typename T>
Shape_1D<int64_t> Bucketize(const Shape_1D<T> &in1, const Shape_1D<T> &in2)
{
    Shape_1D<int64_t> out;
    // quick checks
    if (in2.d0 == 0)
    {
        // no boundaries -> all indices are 0
        out.d0 = in1.d0;
        out.buf.assign(in1.d0, 0);
        return out;
    }
    if (static_cast<int>(in2.buf.size()) != in2.d0)
    {
        throw std::runtime_error("boundaries.d0 mismatch with buffer size");
    }
    if (static_cast<int>(in1.buf.size()) != in1.d0)
    {
        throw std::runtime_error("input.d0 mismatch with buffer size");
    }

    // Ensure boundaries are sorted ascending. If not, you can sort a copy or throw.
    // Here we assume they are sorted; uncomment to enforce:
    // if (!std::is_sorted(in2.buf.begin(), in2.buf.end())) throw std::runtime_error("boundaries must be sorted ascending");

    out.d0 = in1.d0;
    out.buf.resize(out.d0);

    for (int i = 0; i < in1.d0; ++i)
    {
        const T x = in1.buf[i];
        // For right = true use upper_bound: first element > x
        auto it = std::upper_bound(in2.buf.begin(), in2.buf.end(), x);
        int idx = static_cast<int64_t>(std::distance(in2.buf.begin(), it));
        out.buf[i] = idx;
    }

    return out;
}

inline std::size_t idx3(const Shape_3D<float> &s, int c, int h, int w)
{
    return (static_cast<std::size_t>(c) * s.d1 + static_cast<std::size_t>(h)) * s.d2 + static_cast<std::size_t>(w);
}

inline std::size_t idx5(const Shape_5D<float> &s, int n, int a, int b, int c, int d)
{
    return (((static_cast<std::size_t>(n) * s.d1 + static_cast<std::size_t>(a)) * s.d2 + static_cast<std::size_t>(b)) * s.d3 + static_cast<std::size_t>(c)) * s.d4 + static_cast<std::size_t>(d);
}

#endif //DEF_H
