//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//=============================================================================


package com.example.geniechat.model;

import java.util.ArrayList;
import java.util.List;

public class Message {

    public enum Status {
        SENDING,
        SENT,
        FAILED
    }

    public enum AttachmentType {
        IMAGE,
        TEXT_FILE
    }

    public static class Attachment {
        private final AttachmentType type;
        private final String base64Content;
        private final String fileName;

        public Attachment(AttachmentType type, String base64Content, String fileName) {
            this.type = type;
            this.base64Content = base64Content;
            this.fileName = fileName;
        }

        public AttachmentType getType() {
            return type;
        }

        public String getBase64Content() {
            return base64Content;
        }

        public String getFileName() {
            return fileName;
        }
    }

    private String text;
    private boolean isSentByUser;
    private Status status;
    private final long timestamp;
    private List<Attachment> attachments;

    public Message(String text, boolean isSentByUser) {
        this.text = text;
        this.isSentByUser = isSentByUser;
        // AI's messages are considered sent by default
        this.status = isSentByUser ? Status.SENDING : Status.SENT;
        this.timestamp = System.currentTimeMillis();
        this.attachments = new ArrayList<>();
    }

    public Message(String text, boolean isSentByUser, List<Attachment> attachments) {
        this(text, isSentByUser);
        if (attachments != null) {
            this.attachments = new ArrayList<>(attachments);
        }
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

    public List<Attachment> getAttachments() {
        return attachments;
    }

    public void setAttachments(List<Attachment> attachments) {
        this.attachments = attachments;
    }

    public int getImageCount() {
        int count = 0;
        for (Attachment attachment : attachments) {
            if (attachment.getType() == AttachmentType.IMAGE) {
                count++;
            }
        }
        return count;
    }

    public int getTextFileCount() {
        int count = 0;
        for (Attachment attachment : attachments) {
            if (attachment.getType() == AttachmentType.TEXT_FILE) {
                count++;
            }
        }
        return count;
    }

    public boolean hasAttachments() {
        return attachments != null && !attachments.isEmpty();
    }
}
