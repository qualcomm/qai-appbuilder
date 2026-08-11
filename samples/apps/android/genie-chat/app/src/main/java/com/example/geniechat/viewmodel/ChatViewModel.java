//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//=============================================================================

package com.example.geniechat.viewmodel;

import androidx.lifecycle.LiveData;
import androidx.lifecycle.MutableLiveData;
import androidx.lifecycle.ViewModel;

import com.example.geniechat.model.Message;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

public class ChatViewModel extends ViewModel {
    private final MutableLiveData<List<Message>> messagesLiveData = new MutableLiveData<>(new ArrayList<>());
    // Indicates whether the model (assistant) is currently producing a reply.
    // This LiveData lives in the ViewModel so it survives configuration changes
    // (e.g., orientation) and allows the UI to permanently disable menus/input
    // while the assistant is replying.
    private final MutableLiveData<Boolean> isModelReplying = new MutableLiveData<>(false);

    public LiveData<List<Message>> getMessages() {
        return messagesLiveData;
    }

    public LiveData<Boolean> getIsModelReplying() {
        return isModelReplying;
    }

    /**
     * Call when model reply generation starts (e.g., when you send a user message
     * and begin streaming assistant response). This will persist across
     * configuration changes so UI can remain disabled until reply completes.
     */
    public void startModelReply() {
        isModelReplying.postValue(true);
    }

    /**
     * Call when model reply generation finishes or is cancelled. Re-enables UI.
     */
    public void endModelReply() {
        isModelReplying.postValue(false);
    }

    public void addMessage(Message message) {
        List<Message> currentMessages = messagesLiveData.getValue();
        if (currentMessages != null) {
            currentMessages.add(message);
            // This can be called from main thread, but postValue is safe too
            messagesLiveData.postValue(currentMessages);
        }
    }

    public void appendToLastMessage(String chunk) {
        List<Message> currentMessages = messagesLiveData.getValue();
        if (currentMessages != null && !currentMessages.isEmpty()) {
            int lastIndex = currentMessages.size() - 1;
            Message lastMessage = currentMessages.get(lastIndex);
            if (!lastMessage.isSentByUser()) {
                String currentText = lastMessage.getText();
                String newText = "...".equals(currentText) ? chunk : currentText + chunk;
                lastMessage.setText(newText);
                messagesLiveData.postValue(currentMessages); // Notify observers
            }
        }
    }

    public void updateUserMessageStatus(Message userMessage, Message.Status status) {
        List<Message> currentMessages = messagesLiveData.getValue();
        if (currentMessages != null) {
            for (Message msg : currentMessages) {
                if (msg.getTimestamp() == userMessage.getTimestamp()) {
                    msg.setStatus(status);
                    break;
                }
            }
            messagesLiveData.postValue(currentMessages); // Notify observers
        }
    }

    public void retryMessage(Message failedMessage) {
        List<Message> currentMessages = messagesLiveData.getValue();
        if (currentMessages != null) {
            for (Message msg : currentMessages) {
                if (msg.getTimestamp() == failedMessage.getTimestamp()) {
                    msg.setStatus(Message.Status.SENDING);
                    break;
                }
            }
            messagesLiveData.postValue(currentMessages); // Notify observers
        }
    }

    public void setInitialWelcomeMessage() {
        if (messagesLiveData.getValue() == null || messagesLiveData.getValue().isEmpty()) {
            addMessage(new Message("你好！有什么可以帮你的吗？", false));
        }
    }
}
