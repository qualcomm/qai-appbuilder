//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.example.geniechat.adapter;
import android.animation.ValueAnimator;
import android.util.Log;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.view.View.MeasureSpec;
import androidx.annotation.NonNull;
import androidx.core.content.ContextCompat;
import androidx.recyclerview.widget.RecyclerView;
import com.example.geniechat.R;
import com.example.geniechat.model.Message;

import java.util.List;
import java.util.Objects;

import io.noties.markwon.Markwon;
import io.noties.markwon.ext.strikethrough.StrikethroughPlugin;

public class MessageAdapter extends RecyclerView.Adapter<MessageAdapter.MessageViewHolder> {
    private final List<Message> messageList;
    private final Markwon markwon;

    public MessageAdapter(List<Message> messageList) {
        this.messageList = messageList;
        this.markwon = null;
    }

    @NonNull
    @Override
    public MessageViewHolder onCreateViewHolder(@NonNull ViewGroup parent, int viewType) {
        View view = LayoutInflater.from(parent.getContext()).inflate(R.layout.item_message, parent, false);
        return new MessageViewHolder(view, Markwon.builder(parent.getContext())
                .usePlugin(StrikethroughPlugin.create())
                .build());
    }
    @Override public void onBindViewHolder(@NonNull MessageViewHolder holder, int position) {
        holder.bind(messageList.get(position));
    }
    @Override
    public void onBindViewHolder(@NonNull MessageViewHolder holder, int position, @NonNull List<Object> payloads) {
        if (!payloads.isEmpty()) {
            // we use a simple payload marker for incremental text append
            Object p = payloads.get(0);
            if (p instanceof String && "text_append".equals(p)) {
                holder.updateTextAnimated(messageList.get(position).getText());
                return;
            }
        }
        onBindViewHolder(holder, position);
    }
    @Override public int getItemCount() { return messageList.size(); }
    static class MessageViewHolder extends RecyclerView.ViewHolder {
        LinearLayout messageRoot;
        TextView textViewMessage;
        private final Markwon markwon;

        public MessageViewHolder(@NonNull View itemView, Markwon markwon) {
            super(itemView);
            this.markwon = markwon;
            messageRoot = itemView.findViewById(R.id.message_root);
            textViewMessage = itemView.findViewById(R.id.textViewMessage);
        }

        void bind(Message message) {
            // ensure text view doesn't expand beyond a reasonable width (prevent off-screen)
            int parentWidth = itemView.getWidth();
            if (parentWidth > 0) {
                int max = (int) (parentWidth * 0.72f); // 72% of available width
                textViewMessage.setMaxWidth(max);
            } else {
                // fallback: approximate using resources display metrics
                int screenWidth = itemView.getResources().getDisplayMetrics().widthPixels;
                textViewMessage.setMaxWidth((int) (screenWidth * 0.72f));
            }
            // remove default font padding which can cause uneven vertical spacing
            textViewMessage.setIncludeFontPadding(false);

            // Dynamically adjust layout_width based on message content


            if (message.isSentByUser()) {
                textViewMessage.setText(message.getText());
                messageRoot.setGravity(Gravity.END);
                textViewMessage.setBackground(ContextCompat.getDrawable(itemView.getContext(), R.drawable.bg_bubble_right));
            } else {
                markwon.setMarkdown(textViewMessage, message.getText());
                messageRoot.setGravity(Gravity.START);
                textViewMessage.setBackground(ContextCompat.getDrawable(itemView.getContext(), R.drawable.bg_bubble_left));
            }
            // ensure normal layout params after a full bind
            ViewGroup.LayoutParams lp = textViewMessage.getLayoutParams();
            if (lp != null && lp.height != ViewGroup.LayoutParams.WRAP_CONTENT) {
                lp.height = ViewGroup.LayoutParams.WRAP_CONTENT;
                textViewMessage.setLayoutParams(lp);
            }
        }

        /**
         * 平滑动画更新文本高度：测量新文本高度，然后用 ValueAnimator 在旧高度到新高度之间插值，
         * 同时调用 requestLayout()，RecyclerView 会实时重排从而推动历史消息向上而不是被覆盖。
         */
        void updateTextAnimated(String newText) {
            final TextView tv = textViewMessage;
            final CharSequence oldText = tv.getText();
            ViewGroup.LayoutParams params = textViewMessage.getLayoutParams();
            Log.d("TAG", "bind: message.getText() = " + newText);
            if (Objects.equals(newText, "")) {
                params.width = ViewGroup.LayoutParams.WRAP_CONTENT;
            } else {
                params.width = ViewGroup.LayoutParams.MATCH_PARENT;
            }
            textViewMessage.setLayoutParams(params);

            // 记录旧高度
            final int oldHeight = tv.getHeight();

            // 将视图设置为新文本以测量目标高度（使用 markwon 渲染时也能测量）
            if (markwon != null) {
                markwon.setMarkdown(tv, newText);
            } else {
                tv.setText(newText);
            }

            // 强制测量以获取新高度
            int widthSpec = MeasureSpec.makeMeasureSpec(itemView.getWidth(), MeasureSpec.AT_MOST);
            int heightSpec = MeasureSpec.makeMeasureSpec(0, MeasureSpec.UNSPECIFIED);
            tv.measure(widthSpec, heightSpec);
            final int newHeight = tv.getMeasuredHeight();

            // 恢复旧文本和旧高度设置，准备动画
            tv.setText(oldText);
            ViewGroup.LayoutParams tvLp = tv.getLayoutParams();
            if (tvLp == null) return; // should not happen

            // 如果高度没有变化，直接设置文本并返回
            if (newHeight == oldHeight) {
                if (markwon != null) markwon.setMarkdown(tv, newText); else tv.setText(newText);
                tvLp.height = ViewGroup.LayoutParams.WRAP_CONTENT;
                tv.setLayoutParams(tvLp);
                return;
            }

            // 初始化为旧高度（如果为0则测量并使用 measured height）
            if (oldHeight <= 0) {
                tv.measure(widthSpec, MeasureSpec.makeMeasureSpec(0, MeasureSpec.UNSPECIFIED));
            }

            tvLp.height = oldHeight > 0 ? oldHeight : tv.getMeasuredHeight();
            tv.setLayoutParams(tvLp);

            ValueAnimator animator = ValueAnimator.ofInt(tvLp.height, newHeight);
            animator.setDuration(220);
            animator.addUpdateListener(animation -> {
                int val = (Integer) animation.getAnimatedValue();
                ViewGroup.LayoutParams lp = tv.getLayoutParams();
                lp.height = val;
                tv.setLayoutParams(lp);
                // 让父布局重新布局，从而 RecyclerView 可以推动其他 item
                itemView.requestLayout();
            });
            animator.addListener(new android.animation.AnimatorListenerAdapter() {
                @Override
                public void onAnimationEnd(android.animation.Animator animation) {
                    // 动画结束后设置最终文本并恢复 wrap_content
                    if (markwon != null) markwon.setMarkdown(tv, newText); else tv.setText(newText);
                    ViewGroup.LayoutParams lp = tv.getLayoutParams();
                    lp.height = ViewGroup.LayoutParams.WRAP_CONTENT;
                    tv.setLayoutParams(lp);
                }
            });
            animator.start();
        }
    }
}
