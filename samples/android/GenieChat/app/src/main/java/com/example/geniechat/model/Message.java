package com.example.geniechat.model;
public class Message {
    private String text;
    private boolean isSentByUser;
    public Message(String text, boolean isSentByUser) { this.text = text; this.isSentByUser = isSentByUser; }
    public String getText() { return text; }
    public boolean isSentByUser() { return isSentByUser; }
}
