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

import okhttp3.Call;

public class ChatViewModel extends ViewModel {
    public static final String WELCOME_TEXT = "你好！有什么可以帮你的吗？";
    public static final String PLACEHOLDER = "...";
    public static final String STOPPED_MARK = "[已停止]";

    private final MutableLiveData<List<Message>> messagesLiveData = new MutableLiveData<>(new ArrayList<>());
    // Indicates whether the model (assistant) is currently producing a reply.
    // This LiveData lives in the ViewModel so it survives configuration changes
    // (e.g., orientation) and allows the UI to permanently disable menus/input
    // while the assistant is replying.
    private final MutableLiveData<Boolean> isModelReplying = new MutableLiveData<>(false);

    // The in-flight streaming request. Kept here (not in the Activity) so the
    // "stop" button still works after a configuration change.
    private Call activeCall;
    private int activeCallId = 0;

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

    // ---- in-flight request bookkeeping -------------------------------------------------

    /** Starts a new request generation and returns its id. Older callbacks become stale. */
    public synchronized int nextCallId() {
        activeCallId++;
        activeCall = null;
        return activeCallId;
    }

    public synchronized void setActiveCall(int callId, Call call) {
        if (callId == activeCallId) activeCall = call;
    }

    public synchronized boolean isActiveCall(int callId) {
        return callId == activeCallId;
    }

    /**
     * Marks the given request as finished. Returns true only for the first caller
     * while the request is still the active one (so completion is handled once).
     */
    public synchronized boolean finishCall(int callId) {
        if (callId != activeCallId) return false;
        activeCallId++;
        activeCall = null;
        return true;
    }

    /** Invalidates and cancels whatever request is in flight (user pressed "stop"). */
    public synchronized boolean cancelActiveCall() {
        Call c = activeCall;
        activeCallId++;
        activeCall = null;
        if (c != null) {
            c.cancel();
            return true;
        }
        return false;
    }

    // ---- messages ----------------------------------------------------------------------

    /** Main thread only (uses setValue so that callers see the new list immediately). */
    public void setMessages(List<Message> messages) {
        messagesLiveData.setValue(new ArrayList<>(messages));
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
                String newText = PLACEHOLDER.equals(currentText) ? chunk : currentText + chunk;
                lastMessage.setText(newText);
                messagesLiveData.postValue(currentMessages); // Notify observers
            }
        }
    }

    /** Replaces the "..." placeholder with a hint if the model produced no text at all. */
    public void fillEmptyReply(String hint) {
        List<Message> currentMessages = messagesLiveData.getValue();
        if (currentMessages != null && !currentMessages.isEmpty()) {
            Message last = currentMessages.get(currentMessages.size() - 1);
            if (!last.isSentByUser()) {
                String t = last.getText();
                if (t == null || t.trim().isEmpty() || PLACEHOLDER.equals(t)) {
                    last.setText(hint);
                    messagesLiveData.postValue(currentMessages);
                }
            }
        }
    }

    /** Marks the last assistant reply as stopped by the user, keeping the partial text. */
    public void markLastReplyStopped() {
        List<Message> currentMessages = messagesLiveData.getValue();
        if (currentMessages != null && !currentMessages.isEmpty()) {
            Message last = currentMessages.get(currentMessages.size() - 1);
            if (!last.isSentByUser()) {
                String t = last.getText();
                if (t == null || t.trim().isEmpty() || PLACEHOLDER.equals(t)) {
                    last.setText(STOPPED_MARK);
                } else {
                    last.setText(t + "\n\n" + STOPPED_MARK);
                }
                messagesLiveData.postValue(currentMessages);
            }
        }
    }

    public void updateUserMessageStatus(Message userMessage, Message.Status status) {
        List<Message> currentMessages = messagesLiveData.getValue();
        if (currentMessages != null) {
            for (Message msg : currentMessages) {
                if (msg.isSentByUser() && msg.getTimestamp() == userMessage.getTimestamp()) {
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
                if (msg.isSentByUser() && msg.getTimestamp() == failedMessage.getTimestamp()) {
                    msg.setStatus(Message.Status.SENDING);
                    break;
                }
            }
            messagesLiveData.postValue(currentMessages); // Notify observers
        }
    }

    /** Main thread only. */
    public void clearMessages() {
        messagesLiveData.setValue(new ArrayList<>());
    }

    public void setInitialWelcomeMessage() {
        if (messagesLiveData.getValue() == null || messagesLiveData.getValue().isEmpty()) {
            addMessage(new Message(WELCOME_TEXT, false));
        }
    }
}
