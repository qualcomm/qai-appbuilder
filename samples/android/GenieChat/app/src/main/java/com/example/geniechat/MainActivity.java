//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//=============================================================================

package com.example.geniechat;

import android.Manifest;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.Settings;
import android.util.Log;
import android.view.Menu;
import android.view.MenuItem;
import android.widget.EditText;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.lifecycle.ViewModelProvider;
import androidx.preference.PreferenceManager;
import androidx.recyclerview.widget.LinearLayoutManager;
import androidx.recyclerview.widget.RecyclerView;

import com.example.geniechat.adapter.MessageAdapter;
import com.example.geniechat.model.Message;
import com.example.geniechat.viewmodel.ChatViewModel;
import com.google.android.material.floatingactionbutton.FloatingActionButton;
import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.ResponseBody;

public class MainActivity extends AppCompatActivity implements MessageAdapter.OnRetryClickListener {

    private static final String TAG = "MainActivity";
    private static final int STORAGE_PERMISSION_REQUEST_CODE = 101;

    private RecyclerView recyclerView;
    private EditText editTextMessage;
    private FloatingActionButton buttonSend;

    private MessageAdapter messageAdapter;
    private ChatViewModel chatViewModel;
    private OkHttpClient client;
    private Gson gson;
    private MenuItem settingsMenuItem;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        // Initialize ViewModel
        chatViewModel = new ViewModelProvider(this).get(ChatViewModel.class);

        initViews();
        initRecyclerView();
        initHttpClient();

        observeViewModel();

        chatViewModel.setInitialWelcomeMessage();
    }

    private void initViews() {
        recyclerView = findViewById(R.id.recyclerView);
        editTextMessage = findViewById(R.id.editTextMessage);
        buttonSend = findViewById(R.id.buttonSend);
        setSupportActionBar(findViewById(R.id.toolbar));
        buttonSend.setOnClickListener(v -> sendMessage());
    }

    private void initRecyclerView() {
        // Pass an empty list initially, data will be supplied by the ViewModel
        messageAdapter = new MessageAdapter(new ArrayList<>(), this);
        LinearLayoutManager layoutManager = new LinearLayoutManager(this);
        layoutManager.setStackFromEnd(true);
        recyclerView.setLayoutManager(layoutManager);
        recyclerView.setAdapter(messageAdapter);
    }

    private void observeViewModel() {
        chatViewModel.getMessages().observe(this, messages -> {
            messageAdapter.setMessageList(messages);
            if (messages != null && !messages.isEmpty()) {
                recyclerView.scrollToPosition(messages.size() - 1);
            }
        });
    }


    private void initHttpClient() {
        client = new OkHttpClient.Builder()
                .readTimeout(60, TimeUnit.SECONDS)
                .build();
        gson = new Gson();
    }

    @Override
    protected void onResume() {
        super.onResume();
        checkAndRequestStoragePermission();
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.main_menu, menu);
        settingsMenuItem = menu.findItem(R.id.action_settings);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(@NonNull MenuItem item) {
        if (item.getItemId() == R.id.action_settings) {
            Intent intent = new Intent(this, SettingsActivity.class);
            startActivity(intent);
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    private void sendMessage() {
        String messageText = editTextMessage.getText().toString().trim();
        if (messageText.isEmpty()) {
            return;
        }

        Message userMessage = new Message(messageText, true);
        chatViewModel.addMessage(userMessage);
        editTextMessage.setText("");

        setUiEnabled(false); // Disable UI
        callChatApi(userMessage);
    }

    private void setUiEnabled(boolean enabled) {
        runOnUiThread(() -> {
            editTextMessage.setEnabled(enabled);
            buttonSend.setEnabled(enabled);
            buttonSend.setAlpha(enabled ? 1.0f : 0.5f);
            if (settingsMenuItem != null) {
                settingsMenuItem.setEnabled(enabled);
            }
        });
    }

    // This method is now a wrapper around the ViewModel's method.
    private void addMessage(Message message) {
        chatViewModel.addMessage(message);
    }

    // This method is now a wrapper around the ViewModel's method.
    private void appendToLastMessage(String chunk) {
        chatViewModel.appendToLastMessage(chunk);
    }

    private void callChatApi(Message userMessage) {
        // Add a placeholder for the AI's response
        addMessage(new Message("...", false));

        SharedPreferences sharedPreferences = PreferenceManager.getDefaultSharedPreferences(this);
        String ipAddress = sharedPreferences.getString("ip_address", "127.0.0.1:8910");
        String modelName = sharedPreferences.getString("model_name", "qwen2.0_7b");
        float temperature = sharedPreferences.getInt("temperature", 150) / 100.0f;
        float topP = sharedPreferences.getInt("top_p", 60) / 100.0f;
        int topK = sharedPreferences.getInt("top_k", 13);

        String apiUrl = "http://" + ipAddress + "/v1/chat/completions";

        MediaType JSON = MediaType.get("application/json; charset=utf-8");
        JsonObject jsonBody = new JsonObject();
        jsonBody.addProperty("model", modelName);
        jsonBody.addProperty("stream", true);

        JsonArray messages = new JsonArray();
        JsonObject systemMessage = new JsonObject();
        systemMessage.addProperty("role", "system");
        systemMessage.addProperty("content", "You are a helpful assistant.");
        messages.add(systemMessage);

        JsonObject userMsg = new JsonObject();
        userMsg.addProperty("role", "user");
        userMsg.addProperty("content", userMessage.getText());
        messages.add(userMsg);

        jsonBody.add("messages", messages);
        jsonBody.addProperty("temperature", temperature);
        jsonBody.addProperty("top_p", topP);
        jsonBody.addProperty("top_k", topK);

        String requestBodyString = gson.toJson(jsonBody);
        Log.d(TAG, "Request URL: " + apiUrl);
        Log.d(TAG, "Request Body: " + requestBodyString);

        RequestBody body = RequestBody.create(requestBodyString, JSON);
        Request request = new Request.Builder()
                .url(apiUrl)
                .post(body)
                .build();

        client.newCall(request).enqueue(new Callback() {
            @Override
            public void onFailure(@NonNull Call call, @NonNull IOException e) {
                Log.e(TAG, "API Call Failed: ", e);
                chatViewModel.updateUserMessageStatus(userMessage, Message.Status.FAILED);
                appendToLastMessage("\n\n网络错误: " + e.getMessage());
                setUiEnabled(true);
            }

            @Override
            public void onResponse(@NonNull Call call, @NonNull Response response) {
                if (!response.isSuccessful()) {
                    String errorBody = "Unknown error";
                    try {
                        if (response.body() != null) errorBody = response.body().string();
                    } catch (IOException e) {
                        // ignore
                    }
                    Log.e(TAG, "API Call Unsuccessful (" + response.code() + "): " + errorBody);
                    chatViewModel.updateUserMessageStatus(userMessage, Message.Status.FAILED);
                    appendToLastMessage("\n\n请求失败: " + response.code());
                    setUiEnabled(true);
                    return;
                }

                try (ResponseBody responseBody = response.body()) {
                    if (responseBody == null) return;

                    try (BufferedReader reader = new BufferedReader(new InputStreamReader(responseBody.byteStream()))) {
                        String line;
                        while ((line = reader.readLine()) != null) {
                            if (line.startsWith("data:")) {
                                String jsonData = line.substring(5).trim();

                                if ("[DONE]".equals(jsonData)) {
                                    chatViewModel.updateUserMessageStatus(userMessage, Message.Status.SENT);
                                    setUiEnabled(true);
                                    break;
                                }

                                JsonObject chunkObject = gson.fromJson(jsonData, JsonObject.class);
                                JsonArray choices = chunkObject.getAsJsonArray("choices");
                                if (choices != null && !choices.isEmpty()) {
                                    JsonObject delta = choices.get(0).getAsJsonObject().getAsJsonObject("delta");
                                    if (delta != null && delta.has("content")) {
                                        String contentChunk = delta.get("content").getAsString();
                                        appendToLastMessage(contentChunk);
                                    }
                                }
                            }
                        }
                    }

                } catch (Exception e) {
                    Log.e(TAG, "Response processing error: ", e);
                    appendToLastMessage("\n\n处理回复时出错。");
                    chatViewModel.updateUserMessageStatus(userMessage, Message.Status.FAILED);
                    setUiEnabled(true);
                }
            }
        });
    }

    @Override
    public void onRetryClick(int position) {
        List<Message> messages = chatViewModel.getMessages().getValue();
        if (messages != null && position >= 0 && position < messages.size()) {
            Message failedMessage = messages.get(position);
            if (failedMessage.getStatus() == Message.Status.FAILED) {
                chatViewModel.retryMessage(failedMessage);
                setUiEnabled(false);
                callChatApi(failedMessage);
            }
        }
    }

    private void checkAndRequestStoragePermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager()) {
                Intent intent = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION);
                intent.setData(Uri.parse("package:" + getPackageName()));
                startActivity(intent);
            }
        } else {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(this,
                        new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE, Manifest.permission.READ_EXTERNAL_STORAGE},
                        STORAGE_PERMISSION_REQUEST_CODE);
            }
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions, @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == STORAGE_PERMISSION_REQUEST_CODE) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                Log.d(TAG, "Storage permission granted.");
            } else {
                Log.d(TAG, "Storage permission denied.");
            }
        }
    }
}
