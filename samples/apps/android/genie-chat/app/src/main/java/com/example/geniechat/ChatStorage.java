//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//=============================================================================

package com.example.geniechat;

import android.content.Context;
import android.util.Log;

import com.example.geniechat.model.Message;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * Persists the chat history (text only) to the app's private storage so that
 * the conversation survives app restarts. Attachments (image/text file
 * payloads) are intentionally NOT persisted because they can be large.
 */
public final class ChatStorage {

    private static final String TAG = "ChatStorage";
    private static final String FILE_NAME = "chat_history.json";
    private static final int MAX_SAVED_MESSAGES = 200;

    private ChatStorage() {}

    public static synchronized void save(Context context, List<Message> messages) {
        JsonArray array = new JsonArray();
        int start = Math.max(0, messages.size() - MAX_SAVED_MESSAGES);
        for (int i = start; i < messages.size(); i++) {
            Message m = messages.get(i);
            String text = m.getText();
            if (text == null) continue;
            // Skip the transient "..." placeholder of an unfinished reply.
            if (!m.isSentByUser() && "...".equals(text.trim())) continue;
            Message.Status status = m.getStatus();
            // A message still "sending" when we save can never complete after a restart.
            if (status == Message.Status.SENDING) status = Message.Status.FAILED;

            JsonObject o = new JsonObject();
            o.addProperty("text", text);
            o.addProperty("user", m.isSentByUser());
            o.addProperty("status", status.name());
            o.addProperty("ts", m.getTimestamp());
            array.add(o);
        }

        File dir = context.getFilesDir();
        File tmp = new File(dir, FILE_NAME + ".tmp");
        File dst = new File(dir, FILE_NAME);
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            out.write(array.toString().getBytes(StandardCharsets.UTF_8));
            out.getFD().sync();
        } catch (IOException e) {
            Log.e(TAG, "Failed to save chat history", e);
            return;
        }
        if (!tmp.renameTo(dst)) {
            //noinspection ResultOfMethodCallIgnored
            dst.delete();
            if (!tmp.renameTo(dst)) {
                Log.e(TAG, "Failed to replace chat history file");
            }
        }
    }

    public static synchronized List<Message> load(Context context) {
        List<Message> result = new ArrayList<>();
        File f = new File(context.getFilesDir(), FILE_NAME);
        if (!f.exists()) return result;
        try (FileInputStream in = new FileInputStream(f)) {
            byte[] buf = new byte[(int) f.length()];
            int off = 0;
            while (off < buf.length) {
                int n = in.read(buf, off, buf.length - off);
                if (n < 0) break;
                off += n;
            }
            JsonArray array = JsonParser.parseString(new String(buf, 0, off, StandardCharsets.UTF_8)).getAsJsonArray();
            for (JsonElement e : array) {
                JsonObject o = e.getAsJsonObject();
                Message.Status status;
                try {
                    status = Message.Status.valueOf(o.get("status").getAsString());
                } catch (Exception ex) {
                    status = Message.Status.SENT;
                }
                result.add(new Message(
                        o.get("text").getAsString(),
                        o.get("user").getAsBoolean(),
                        status,
                        o.get("ts").getAsLong()));
            }
        } catch (Exception e) {
            Log.e(TAG, "Failed to load chat history", e);
            result.clear();
        }
        return result;
    }

    public static synchronized void clear(Context context) {
        //noinspection ResultOfMethodCallIgnored
        new File(context.getFilesDir(), FILE_NAME).delete();
    }
}
