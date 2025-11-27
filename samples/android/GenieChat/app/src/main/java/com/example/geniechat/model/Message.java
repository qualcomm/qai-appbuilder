//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//=============================================================================

package com.example.geniechat.model;

public class Message {

    public enum Status {
        SENDING,
        SENT,
        FAILED
    }

    private String text;
    private boolean isSentByUser;
    private Status status;
    private final long timestamp;

    public Message(String text, boolean isSentByUser) {
        this.text = text;
        this.isSentByUser = isSentByUser;
        // AI's messages are considered sent by default
        this.status = isSentByUser ? Status.SENDING : Status.SENT;
        this.timestamp = System.currentTimeMillis();
    }

    public long getTimestamp() {
        return timestamp;
    }

    public void setText(String text) {
        this.text = text;
    }

    public String getText() {
        return text;
    }

    public boolean isSentByUser() {
        return isSentByUser;
    }

    public Status getStatus() {
        return status;
    }

    public void setStatus(Status status) {
        this.status = status;
    }
}
