//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.example.geniechat.adapter;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.TextView;
import androidx.annotation.NonNull;
import androidx.core.content.ContextCompat;
import androidx.recyclerview.widget.RecyclerView;
import com.example.geniechat.R;
import com.example.geniechat.model.Message;

import java.util.List;

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
            if (message.isSentByUser()) {
                textViewMessage.setText(message.getText());
                messageRoot.setGravity(Gravity.END);
                textViewMessage.setBackground(ContextCompat.getDrawable(itemView.getContext(), R.drawable.bg_bubble_right));
            } else {
                markwon.setMarkdown(textViewMessage, message.getText());
                messageRoot.setGravity(Gravity.START);
                textViewMessage.setBackground(ContextCompat.getDrawable(itemView.getContext(), R.drawable.bg_bubble_left));
            }
        }
    }
}
