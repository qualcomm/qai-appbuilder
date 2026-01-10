// phi4mm_image_processor.hpp
// Header-only facade for Phi4MM image preprocessing pipeline.
// Keep STB implementations in exactly ONE .cpp (see demo.cpp).
#pragma once
#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <string>
#include <tuple>
#include <vector>

// External dependencies (declarations only if STB_*_IMPLEMENTATION not defined before inclusion)

namespace phi4mm {

struct ImageRGB8 {
    int w = 0, h = 0, c = 3;                // interleaved RGB, c=3
    std::vector<uint8_t> data;
};

struct TensorF32 {
    // NCHW float32
    int N = 0, C = 0, H = 0, W = 0;
    std::vector<float> data;                // size = N*C*H*W
};

struct DynHDResult {
    ImageRGB8 resized_padded;               // final target size, white padded (RGB=255)
    int pad_w = 0, pad_h = 0;
    int target_w = 0, target_h = 0;
    int grid_w = 0, grid_h = 0;             // number of 448x448 tiles
    std::vector<uint8_t> attn_mask;         // mask size (mask_h x mask_w), values {0,1}
    int mask_w = 0, mask_h = 0;
};

// Optional: pipeline output container
struct PipelineOutput {
    TensorF32 image_inputs;                 // [N,3,448,448] first=global, followed by local tiles
    std::vector<int> num_img_tokens;        // per-local tile tokens (global excluded)
    DynHDResult dyn;                        // dynamic preprocess meta/output
};

class Phi4MMImageProcessor {
public:
    // ------------ I/O ------------
    static bool loadImageRGB8_ByMEM(uint8_t *png_bin_buf, unsigned long dwLen, ImageRGB8& out) {
        int w=0, h=0, comp=0;
        unsigned char *pixels = stbi_load_from_memory(png_bin_buf, dwLen, &w, &h, &comp, 0);
        if (!pixels) return false;
        out.w = w; out.h = h; out.c = 3;
        out.data.assign(pixels, pixels + (size_t)w*h*3);
        stbi_image_free(pixels);
        return true;
    }

    static bool loadImageRGB8(const std::string& path, ImageRGB8& out) {
        int w=0, h=0, comp=0;
        uint8_t* pixels = stbi_load(path.c_str(), &w, &h, &comp, 3);
        if (!pixels) return false;
        out.w = w; out.h = h; out.c = 3;
        out.data.assign(pixels, pixels + (size_t)w*h*3);
        stbi_image_free(pixels);
        return true;
    }

    static bool savePNG(const std::string& path, const ImageRGB8& img) {
        return stbi_write_png(path.c_str(), img.w, img.h, 3, img.data.data(), img.w*3) != 0;
    }

    // ------------ Resize (nearest / bilinear / bicubic) ------------
    static ImageRGB8 resizeNearest(const ImageRGB8& src, int dst_w, int dst_h) {
        ImageRGB8 dst; dst.w=dst_w; dst.h=dst_h; dst.c=3;
        dst.data.resize((size_t)dst_w*dst_h*3);
        const double sx = (double)src.w/dst_w;
        const double sy = (double)src.h/dst_h;
        for (int y=0; y<dst_h; ++y) {
            int syi = (int)std::floor((y + 0.5) * sy);
            syi = clamp_i(syi, 0, src.h - 1);
            for (int x=0; x<dst_w; ++x) {
                int sxi = (int)std::floor((x + 0.5) * sx);
                sxi = clamp_i(sxi, 0, src.w - 1);
                const uint8_t* sp = &src.data[(size_t)(syi*src.w + sxi)*3];
                uint8_t* dp = &dst.data[(size_t)(y*dst_w + x)*3];
                dp[0]=sp[0]; dp[1]=sp[1]; dp[2]=sp[2];
            }
        }
        return dst;
    }

    static ImageRGB8 resizeBilinear(const ImageRGB8& src, int dst_w, int dst_h, bool align_corners=false) {
        ImageRGB8 dst; dst.w=dst_w; dst.h=dst_h; dst.c=3;
        dst.data.resize((size_t)dst_w*dst_h*3);
        const float scale_x = align_corners ? (src.w - 1.f) / (dst_w - 1.f) : (float)src.w / dst_w;
        const float scale_y = align_corners ? (src.h - 1.f) / (dst_h - 1.f) : (float)src.h / dst_h;
        for (int y=0; y<dst_h; ++y) {
            float sy = align_corners ? y*scale_y : ((y + 0.5f)*scale_y - 0.5f);
            for (int x=0; x<dst_w; ++x) {
                float sx = align_corners ? x*scale_x : ((x + 0.5f)*scale_x - 0.5f);
                auto rgb = sample_bilinear_rgb8(src, sx, sy);
                uint8_t* dp = &dst.data[(size_t)(y*dst_w + x)*3];
                dp[0] = (uint8_t)std::lround(std::min(255.f, std::max(0.f, rgb[0])));
                dp[1] = (uint8_t)std::lround(std::min(255.f, std::max(0.f, rgb[1])));
                dp[2] = (uint8_t)std::lround(std::min(255.f, std::max(0.f, rgb[2])));
            }
        }
        return dst;
    }

    static ImageRGB8 resizeBicubicU8(const ImageRGB8& src, int dst_w, int dst_h,
                                     float a=-0.75f, bool align_corners=false) {
        ImageRGB8 dst; dst.w=dst_w; dst.h=dst_h; dst.c=3;
        dst.data.resize((size_t)dst_w*dst_h*3);
        const float scale_x = align_corners ? (src.w - 1.f) / (dst_w - 1.f) : (float)src.w / dst_w;
        const float scale_y = align_corners ? (src.h - 1.f) / (dst_h - 1.f) : (float)src.h / dst_h;
        for (int y=0; y<dst_h; ++y) {
            float sy = align_corners ? y*scale_y : ((y + 0.5f)*scale_y - 0.5f);
            for (int x=0; x<dst_w; ++x) {
                float sx = align_corners ? x*scale_x : ((x + 0.5f)*scale_x - 0.5f);
                auto rgb = sample_bicubic_rgb8(src, sx, sy, a);
                uint8_t* dp = &dst.data[(size_t)(y*dst_w + x)*3];
                dp[0] = (uint8_t)std::lround(rgb[0]);
                dp[1] = (uint8_t)std::lround(rgb[1]);
                dp[2] = (uint8_t)std::lround(rgb[2]);
            }
        }
        return dst;
    }

    // ------------ Normalize / tensor ------------
    static TensorF32 toTensorNormalized(const ImageRGB8& img) {
        TensorF32 T; T.N=1; T.C=3; T.H=img.h; T.W=img.w;
        T.data.resize((size_t)T.C*T.H*T.W);
        const size_t plane = (size_t)T.H*T.W;
        for (int y=0; y<img.h; ++y) {
            for (int x=0; x<img.w; ++x) {
                const uint8_t* p = &img.data[(size_t)(y*img.w + x)*3];
                T.data[0*plane + (size_t)y*T.W + x] = norm01_to_m11(p[0]);
                T.data[1*plane + (size_t)y*T.W + x] = norm01_to_m11(p[1]);
                T.data[2*plane + (size_t)y*T.W + x] = norm01_to_m11(p[2]);
            }
        }
        return T;
    }

    // Bicubic float path: directly sample float (no uint8-roundtrip), then normalize to [-1,1]
    static TensorF32 resizeBicubicToTensorNormalized(const ImageRGB8& src, int dst_w, int dst_h,
                                                     float a=-0.75f, bool align_corners=false) {
        TensorF32 T; T.N=1; T.C=3; T.H=dst_h; T.W=dst_w;
        T.data.resize((size_t)T.C * T.H * T.W);
        const size_t plane = (size_t)T.H * T.W;
        const float scale_x = align_corners ? (src.w - 1.f) / (dst_w - 1.f) : (float)src.w / dst_w;
        const float scale_y = align_corners ? (src.h - 1.f) / (dst_h - 1.f) : (float)src.h / dst_h;
        for (int y=0; y<dst_h; ++y) {
            float sy = align_corners ? y*scale_y : ((y + 0.5f)*scale_y - 0.5f);
            for (int x=0; x<dst_w; ++x) {
                float sx = align_corners ? x*scale_x : ((x + 0.5f)*scale_x - 0.5f);
                auto rgb = sample_bicubic_rgb8_noclamp(src, sx, sy, a);
                float r = rgb[0] / 127.5f - 1.0f;
                float g = rgb[1] / 127.5f - 1.0f;
                float b = rgb[2] / 127.5f - 1.0f;
                T.data[0*plane + (size_t)y*T.W + x] = r;
                T.data[1*plane + (size_t)y*T.W + x] = g;
                T.data[2*plane + (size_t)y*T.W + x] = b;
            }
        }
        return T;
    }

    // ------------ Phi4MM Dynamic-HD preprocess ------------
    static DynHDResult dynamicPreprocess(const ImageRGB8& input,
                                         int max_num,
                                         int image_size = 448,
                                         int mask_size  = 32,
                                         bool /*use_thumbnail*/ = true) {
        DynHDResult R;
        const int orig_w = input.w, orig_h = input.h;
        int w_crop = (int)std::ceil(orig_w / (float)image_size);
        int h_crop = (int)std::ceil(orig_h / (float)image_size);

        int target_w = 0, target_h = 0;
        std::pair<int,int> target_ar;
        if (w_crop * h_crop > max_num) {
            // enumerate ratios with i*j in [1, max_num]
            std::vector<std::pair<int,int>> ratios;
            for (int n=1; n<=max_num; ++n)
                for (int i=1; i<=n; ++i)
                    for (int j=1; j<=n; ++j)
                        if (i*j <= max_num && i*j >= 1) ratios.emplace_back(i,j);

			std::sort(ratios.begin(), ratios.end(),
					  [](const std::pair<int,int>& a, const std::pair<int,int>& b) { return a.first*a.second < b.first*b.second; });

            float aspect = (float)orig_w / (float)orig_h;
            target_ar = findClosestAspectRatio(aspect, ratios, orig_w, orig_h, image_size);
            target_w = image_size * target_ar.first;
            target_h = image_size * target_ar.second;
        } else {
            target_w = image_size * w_crop;
            target_h = image_size * h_crop;
            target_ar = { w_crop, h_crop };
        }

        // scale-to-fit and pad right/bottom with white (255)
        double ratio_w = (double)target_w / orig_w;
        double ratio_h = (double)target_h / orig_h;
        int new_w=0, new_h=0, pad_w=0, pad_h=0;
        if (ratio_w < ratio_h) {
            new_w = target_w;
            new_h = (int)(orig_h * ratio_w); // Python int(): trunc -> floor for positive
            pad_w = 0;
            pad_h = target_h - new_h;
        } else {
            new_w = (int)(orig_w * ratio_h);
            new_h = target_h;
            pad_w = target_w - new_w;
            pad_h = 0;
        }

        ImageRGB8 resized = resizeBilinear(input, new_w, new_h, false);
        R.resized_padded.w = target_w; R.resized_padded.h = target_h; R.resized_padded.c = 3;
        R.resized_padded.data.assign((size_t)target_w*target_h*3, 255u);

        for (int y=0; y<new_h; ++y)
            for (int x=0; x<new_w; ++x) {
                const uint8_t* sp = &resized.data[(size_t)(y*new_w + x)*3];
                uint8_t* dp = &R.resized_padded.data[(size_t)(y*target_w + x)*3];
                dp[0]=sp[0]; dp[1]=sp[1]; dp[2]=sp[2];
            }

        // attention mask (mask_size * target_ar)
        const int mask_w = mask_size * target_ar.first;
        const int mask_h = mask_size * target_ar.second;
        R.attn_mask.assign((size_t)mask_w * mask_h, 1);

        // if padding >= 14 px, zero out last columns/rows
        if (pad_w >= 14) {
            int cols_zero = pad_w / 14;
            for (int y=0; y<mask_h; ++y)
                for (int x=mask_w - cols_zero; x<mask_w; ++x)
                    R.attn_mask[(size_t)y*mask_w + x] = 0;
        }
        if (pad_h >= 14) {
            int rows_zero = pad_h / 14;
            for (int y=mask_h - rows_zero; y<mask_h; ++y)
                for (int x=0; x<mask_w; ++x)
                    R.attn_mask[(size_t)y*mask_w + x] = 0;
        }

        // meta
        R.pad_w = pad_w; R.pad_h = pad_h;
        R.target_w = target_w; R.target_h = target_h;
        R.grid_w = target_w / image_size;
        R.grid_h = target_h / image_size;
        R.mask_w = mask_w; R.mask_h = mask_h;
        return R;
    }

    // ------------ Tiles 448x448 ------------
    static std::vector<ImageRGB8> extractTiles448(const ImageRGB8& img448grid) {
        const int TILE = 448;
        std::vector<ImageRGB8> crops;
        for (int gy=0; gy<img448grid.h; gy+=TILE)
            for (int gx=0; gx<img448grid.w; gx+=TILE) {
                ImageRGB8 tile; tile.w=TILE; tile.h=TILE; tile.c=3;
                tile.data.resize((size_t)TILE*TILE*3);
                for (int y=0; y<TILE; ++y)
                    for (int x=0; x<TILE; ++x) {
                        const uint8_t* sp = &img448grid.data[(size_t)((gy+y)*img448grid.w + (gx+x))*3];
                        uint8_t* dp = &tile.data[(size_t)(y*TILE + x)*3];
                        dp[0]=sp[0]; dp[1]=sp[1]; dp[2]=sp[2];
                    }
                crops.push_back(std::move(tile));
            }
        return crops;
    }

    // ------------ Stack crops to NCHW normalized ------------
    static TensorF32 stackCropsNCHWNormalized(const std::vector<ImageRGB8>& crops) {
        assert(!crops.empty());
        TensorF32 T; T.N = (int)crops.size(); T.C=3; T.H=448; T.W=448;
        T.data.resize((size_t)T.N*T.C*T.H*T.W);
        const size_t crop_stride = (size_t)T.C*T.H*T.W;
        for (int i=0; i<T.N; ++i) {
            TensorF32 one = toTensorNormalized(crops[i]); // 1x3x448x448
            std::copy(one.data.begin(), one.data.end(), T.data.begin() + (size_t)i*crop_stride);
        }
        return T;
    }

    // ------------ Downsample masks & num_img_tokens ------------
    static std::vector<int> computeNumImgTokens(const DynHDResult& R,
                                                int mask_res = 32,
                                                int /*base_res*/ = 448) {
        std::vector<int> result;
        const int tiles_w = R.grid_w, tiles_h = R.grid_h;
        const int tile_mask = mask_res / 2 + mask_res % 2; // 16 for 32

        for (int ty=0; ty<tiles_h; ++ty) {
            for (int tx=0; tx<tiles_w; ++tx) {
                int start_y = ty * mask_res;
                int start_x = tx * mask_res;

                std::vector<uint8_t> tile((size_t)tile_mask * tile_mask, 1);
                for (int y=0; y<tile_mask; ++y)
                    for (int x=0; x<tile_mask; ++x) {
                        int yy = start_y + y*2;
                        int xx = start_x + x*2;
                        yy = std::min(yy, R.mask_h - 1);
                        xx = std::min(xx, R.mask_w - 1);
                        tile[(size_t)y*tile_mask + x] = R.attn_mask[(size_t)yy*R.mask_w + xx];
                    }

                int sum_all = 0, sum_col0 = 0;
                for (int y=0; y<tile_mask; ++y) {
                    for (int x=0; x<tile_mask; ++x) sum_all += tile[(size_t)y*tile_mask + x];
                    sum_col0 += tile[(size_t)y*tile_mask + 0];
                }
                int num_tokens = 256 + 1 + sum_all + sum_col0 + 16;
                result.push_back(num_tokens);
            }
        }
        return result;
    }

    // ------------ NPY / RAW writers ------------
    static void writeNPYFloat32(const std::string& path, const TensorF32& T) {
        std::ofstream f(path, std::ios::binary);
        if (!f) { std::cerr << "Failed to open " << path << "\n"; return; }
        std::string header = "{'descr': '<f4', 'fortran_order': False, 'shape': ("
            + std::to_string(T.N) + ", " + std::to_string(T.C) + ", "
            + std::to_string(T.H) + ", " + std::to_string(T.W) + "), }";
        const char magic[] = "\x93NUMPY";
        uint8_t ver_major = 1, ver_minor = 0;

        size_t header_len = header.size() + 1; // + newline
        size_t pad = 16 - ((10 + header_len) % 16);
        if (pad == 16) pad = 0;
        header.append(pad, ' ');
        header.push_back('\n');
        uint16_t hlen_le = (uint16_t)header.size();

        f.write(magic, 6);
        f.put((char)ver_major);
        f.put((char)ver_minor);
        f.write((char*)&hlen_le, sizeof(uint16_t));
        f.write(header.data(), header.size());

        f.write(reinterpret_cast<const char*>(T.data.data()),
                (std::streamsize)T.data.size() * sizeof(float));
        f.close();
        std::cout << "Saved NPY: " << path << " shape=("
                  << T.N << "," << T.C << "," << T.H << "," << T.W << ")\n";
    }

    static void writeRawFloat32(const std::string& path, const TensorF32& T) {
        std::ofstream f(path, std::ios::binary);
        if (!f) { std::cerr << "Failed to open " << path << "\n"; return; }
        f.write(reinterpret_cast<const char*>(T.data.data()),
                static_cast<std::streamsize>(T.data.size() * sizeof(float)));
        f.close();
        std::cout << "Saved RAW (float32): " << path << " bytes="
                  << (T.data.size() * sizeof(float)) << " order=NCHW\n";
    }

    // ------------ One-shot pipeline (helper) ------------
    // Returns [N,3,448,448] where N = 1 (global) + tiles; also returns tile num_img_tokens.
    static PipelineOutput runPipeline(const ImageRGB8& input,
                                      int dynamic_hd = 12,
                                      int base_res    = 448,
                                      int mask_res    = 32) {
        PipelineOutput P;
        P.dyn = dynamicPreprocess(input, dynamic_hd, base_res, mask_res, true);

        // Local crops (448x448)
        std::vector<ImageRGB8> local = extractTiles448(P.dyn.resized_padded);

        // Global crop via float bicubic path
        TensorF32 global_tensor = resizeBicubicToTensorNormalized(P.dyn.resized_padded,
                                                                  base_res, base_res, -0.75f, false);

        // Final stack: [1 (global) + local_count, 3, 448, 448]
        TensorF32 image_inputs;
        image_inputs.N = (int)local.size() + 1;
        image_inputs.C = 3;
        image_inputs.H = base_res;
        image_inputs.W = base_res;
        image_inputs.data.resize((size_t)image_inputs.N * image_inputs.C * image_inputs.H * image_inputs.W);
        const size_t crop_stride = (size_t)image_inputs.C * image_inputs.H * image_inputs.W;

        std::copy(global_tensor.data.begin(), global_tensor.data.end(), image_inputs.data.begin());
        for (int i=0; i<(int)local.size(); ++i) {
            TensorF32 one = toTensorNormalized(local[i]); // 1x3x448x448
            std::copy(one.data.begin(), one.data.end(),
                      image_inputs.data.begin() + (size_t)(i+1) * crop_stride);
        }
        P.image_inputs = std::move(image_inputs);

        // Num tokens per local tile
        P.num_img_tokens = computeNumImgTokens(P.dyn, mask_res, base_res);
        return P;
    }

private:
    // ------------ helpers ------------
    static inline uint8_t clamp_u8(int v) {
        return (uint8_t)std::max(0, std::min(255, v));
    }
    static inline int clamp_i(int v, int lo, int hi) {
        return std::max(lo, std::min(hi, v));
    }
    static inline float norm01_to_m11(uint8_t v) {
        return (float)v / 127.5f - 1.0f;
    }

    static inline std::array<float,3> sample_bilinear_rgb8(const ImageRGB8& src, float sx, float sy) {
        int x0 = (int)std::floor(sx);
        int y0 = (int)std::floor(sy);
        float dx = sx - x0;
        float dy = sy - y0;
        int x1 = x0 + 1; int y1 = y0 + 1;
        x0 = clamp_i(x0, 0, src.w-1); x1 = clamp_i(x1, 0, src.w-1);
        y0 = clamp_i(y0, 0, src.h-1); y1 = clamp_i(y1, 0, src.h-1);
        const uint8_t* p00 = &src.data[(size_t)(y0*src.w + x0)*3];
        const uint8_t* p10 = &src.data[(size_t)(y0*src.w + x1)*3];
        const uint8_t* p01 = &src.data[(size_t)(y1*src.w + x0)*3];
        const uint8_t* p11 = &src.data[(size_t)(y1*src.w + x1)*3];
        float w00 = (1.0f - dx) * (1.0f - dy);
        float w10 = dx * (1.0f - dy);
        float w01 = (1.0f - dx) * dy;
        float w11 = dx * dy;
        std::array<float,3> out = {0.f, 0.f, 0.f};
        for (int c=0;c<3;++c) {
            out[c] = w00*p00[c] + w10*p10[c] + w01*p01[c] + w11*p11[c];
        }
        return out;
    }

    static inline float cubic_weight(float x, float a) {
        x = std::fabs(x);
        if (x <= 1.0f) return ((a + 2.0f) * x - (a + 3.0f)) * x * x + 1.0f;
        if (x < 2.0f)  return (((x - 5.0f) * x + 8.0f) * x - 4.0f) * a;
        return 0.0f;
    }

    static inline std::array<float,3> sample_bicubic_rgb8(const ImageRGB8& src, float sx, float sy, float a) {
        int ix = (int)std::floor(sx);
        int iy = (int)std::floor(sy);
        std::array<float,3> out = {0.f, 0.f, 0.f};
        for (int m=-1; m<=2; ++m) {
            float wy = cubic_weight((iy + m) - sy, a);
            int yy = clamp_i(iy + m, 0, src.h - 1);
            for (int n=-1; n<=2; ++n) {
                float wx = cubic_weight((ix + n) - sx, a);
                int xx = clamp_i(ix + n, 0, src.w - 1);
                const uint8_t* sp = &src.data[(size_t)(yy*src.w + xx)*3];
                float w = wx * wy;
                out[0] += w * sp[0];
                out[1] += w * sp[1];
                out[2] += w * sp[2];
            }
        }
        // clamp [0,255]
        out[0] = std::min(255.f, std::max(0.f, out[0]));
        out[1] = std::min(255.f, std::max(0.f, out[1]));
        out[2] = std::min(255.f, std::max(0.f, out[2]));
        return out;
    }

    static inline std::array<float,3> sample_bicubic_rgb8_noclamp(const ImageRGB8& src, float sx, float sy, float a) {
        int ix = (int)std::floor(sx);
        int iy = (int)std::floor(sy);
        std::array<float,3> out = {0.f, 0.f, 0.f};
        for (int m=-1; m<=2; ++m) {
            float wy = cubic_weight((iy + m) - sy, a);
            int yy = clamp_i(iy + m, 0, src.h - 1);
            for (int n=-1; n<=2; ++n) {
                float wx = cubic_weight((ix + n) - sx, a);
                int xx = clamp_i(ix + n, 0, src.w - 1);
                const uint8_t* sp = &src.data[(size_t)(yy*src.w + xx)*3];
                float w = wx * wy;
                out[0] += w * sp[0];
                out[1] += w * sp[1];
                out[2] += w * sp[2];
            }
        }
        return out;
    }

    static std::pair<int,int> findClosestAspectRatio(
        float aspect,
        const std::vector<std::pair<int,int>>& target_ratios,
        int orig_w, int orig_h, int image_size) {
        float best_diff = std::numeric_limits<float>::infinity();
        std::pair<int,int> best = {1,1};
        double area = (double)orig_w * orig_h;
        for (auto r : target_ratios) {
            float t = (float)r.first / (float)r.second;
            float diff = std::fabs(aspect - t);
            if (diff < best_diff) { best_diff = diff; best = r; }
            else if (diff == best_diff) {
                if (area > 0.5 * image_size * image_size * r.first * r.second) {
                    best = r;
                }
            }
        }
        return best;
       }
};
}