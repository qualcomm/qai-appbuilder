//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//=============================================================================

package com.example.geniechat.view;

import android.content.Context;
import android.util.AttributeSet;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;
import androidx.appcompat.widget.AppCompatTextView;

public class PaddedTextView extends AppCompatTextView {

    public PaddedTextView(@NonNull Context context) {
        super(context);
    }

    public PaddedTextView(@NonNull Context context, @Nullable AttributeSet attrs) {
        super(context, attrs);
    }

    public PaddedTextView(@NonNull Context context, @Nullable AttributeSet attrs, int defStyleAttr) {
        super(context, attrs, defStyleAttr);
    }

    @Override
    protected void onMeasure(int widthMeasureSpec, int heightMeasureSpec) {
        // 1. 首先，让父类（原始的 TextView）完成它所有复杂的测量工作。
        //    这样我们就得到了它在 wrap_content 下计算出的“正常”尺寸。
        super.onMeasure(widthMeasureSpec, heightMeasureSpec);

        // 2. 获取父类测量后的高度和宽度。
        int measuredWidth = getMeasuredWidth();
        int measuredHeight = getMeasuredHeight();

        // 3. 获取当前字体下单行文本的高度。
        int oneLineHeight = 15;

        // 4. 计算我们最终想要的高度：原始高度 + 一行的高度。
        int finalHeight = measuredHeight + oneLineHeight;

        // 5. 将我们最终计算出的尺寸（宽度不变，高度增加）报告给布局系统。
        //    这是 onMeasure() 方法的最终使命。
        setMeasuredDimension(measuredWidth, finalHeight);
    }
}