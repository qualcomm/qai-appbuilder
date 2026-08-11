//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//=============================================================================

package com.example.geniechat;

import android.Manifest;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.OpenableColumns;
import android.provider.Settings;
import android.util.Base64;
import android.util.Log;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;
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
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
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
    private static final int PICK_IMAGE_REQUEST_CODE = 102;
    private static final int PICK_DOCUMENT_REQUEST_CODE = 103;
    private static final long MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB limit

    private RecyclerView recyclerView;
    private EditText editTextMessage;
    private FloatingActionButton buttonSend;
    private ImageView buttonAddAttachment;
    

    private LinearLayout attachmentPreviewContainer;
    private LinearLayout imageAttachmentIndicator;
    private LinearLayout docAttachmentIndicator;
    private TextView imageCountText;
    private TextView docCountText;
    private ImageView clearImagesButton;
    private ImageView clearDocsButton;

    private MessageAdapter messageAdapter;
    private ChatViewModel chatViewModel;
    private OkHttpClient client;
    private Gson gson;
    private MenuItem settingsMenuItem;
    

    private List<Message.Attachment> currentAttachments = new ArrayList<>();

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
        buttonAddAttachment = findViewById(R.id.buttonAddAttachment);
        

        attachmentPreviewContainer = findViewById(R.id.attachmentPreviewContainer);
        imageAttachmentIndicator = findViewById(R.id.imageAttachmentIndicator);
        docAttachmentIndicator = findViewById(R.id.docAttachmentIndicator);
        imageCountText = findViewById(R.id.imageCountText);
        docCountText = findViewById(R.id.docCountText);
        clearImagesButton = findViewById(R.id.clearImagesButton);
        clearDocsButton = findViewById(R.id.clearDocsButton);
        
        setSupportActionBar(findViewById(R.id.toolbar));
        buttonSend.setOnClickListener(v -> sendMessage());
        buttonAddAttachment.setOnClickListener(v -> showAttachmentDialog());
        

        clearImagesButton.setOnClickListener(v -> clearImageAttachments());
        clearDocsButton.setOnClickListener(v -> clearDocAttachments());
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
        // Observe model reply state so UI (menus / input) can remain disabled
        // across configuration changes while the assistant is replying.
        chatViewModel.getIsModelReplying().observe(this, isReplying -> {
            boolean enabled = !(isReplying != null && isReplying);
            setUiEnabled(enabled);
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
        // Ensure the menu item's enabled state reflects current model-reply state
        // (the ViewModel value survives configuration changes).
        boolean enabled = !Boolean.TRUE.equals(chatViewModel.getIsModelReplying().getValue());
        settingsMenuItem.setEnabled(enabled);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(@NonNull MenuItem item) {
        if (item.getItemId() == R.id.action_settings) {
            // Prevent opening Settings while the model is replying.
            if (Boolean.TRUE.equals(chatViewModel.getIsModelReplying().getValue())) {
                Toast.makeText(this, "Generating, cannot select settings", Toast.LENGTH_SHORT).show();
                return true;
            }
            Intent intent = new Intent(this, SettingsActivity.class);
            startActivity(intent);
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    private void sendMessage() {
        String messageText = editTextMessage.getText().toString().trim();
        if (messageText.isEmpty() && currentAttachments.isEmpty()) {
            return;
        }


        Message userMessage;
        if (!currentAttachments.isEmpty()) {
            userMessage = new Message(messageText, true, new ArrayList<>(currentAttachments));
        } else {
            userMessage = new Message(messageText, true);
        }
        
        chatViewModel.addMessage(userMessage);
        editTextMessage.setText("");
        clearAllAttachments();

        // Immediately disable UI and persist model-reply state so it survives
        // orientation changes.
        setUiEnabled(false);
        chatViewModel.startModelReply();
        callChatApi(userMessage);
    }

    private void setUiEnabled(boolean enabled) {
        runOnUiThread(() -> {
            editTextMessage.setEnabled(enabled);
            buttonSend.setEnabled(enabled);
            buttonSend.setAlpha(enabled ? 1.0f : 0.5f);
            buttonAddAttachment.setEnabled(enabled);
            buttonAddAttachment.setAlpha(enabled ? 1.0f : 0.5f);
            if (settingsMenuItem != null) {
                settingsMenuItem.setEnabled(enabled);
            }
        });
    }


    private void showAttachmentDialog() {
        String[] options = {
            getString(R.string.select_image),
            getString(R.string.select_document)
        };
        
        new AlertDialog.Builder(this)
            .setTitle(R.string.select_attachment_type)
            .setItems(options, (dialog, which) -> {
                if (which == 0) {
                    pickImage();
                } else {
                    pickDocument();
                }
            })
            .setNegativeButton(R.string.cancel, null)
            .show();
    }

    private void pickImage() {
        Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
        intent.setType("image/*");
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        startActivityForResult(Intent.createChooser(intent, getString(R.string.select_image)), PICK_IMAGE_REQUEST_CODE);
    }

    private void pickDocument() {
        Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
        intent.setType("text/plain");
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        startActivityForResult(Intent.createChooser(intent, getString(R.string.select_document)), PICK_DOCUMENT_REQUEST_CODE);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, @Nullable Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        
        if (resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        
        Uri uri = data.getData();
        
        if (requestCode == PICK_IMAGE_REQUEST_CODE) {
            handleImageSelection(uri);
        } else if (requestCode == PICK_DOCUMENT_REQUEST_CODE) {
            handleDocumentSelection(uri);
        }
    }

    private void handleImageSelection(Uri uri) {
        try {

            long fileSize = getFileSize(uri);
            if (fileSize > MAX_FILE_SIZE) {
                Toast.makeText(this, R.string.file_too_large, Toast.LENGTH_SHORT).show();
                return;
            }
            

            InputStream inputStream = getContentResolver().openInputStream(uri);
            if (inputStream == null) {
                Toast.makeText(this, R.string.file_read_error, Toast.LENGTH_SHORT).show();
                return;
            }
            
            ByteArrayOutputStream byteArrayOutputStream = new ByteArrayOutputStream();
            byte[] buffer = new byte[4096];
            int bytesRead;
            while ((bytesRead = inputStream.read(buffer)) != -1) {
                byteArrayOutputStream.write(buffer, 0, bytesRead);
            }
            inputStream.close();
            
            String base64Image = Base64.encodeToString(byteArrayOutputStream.toByteArray(), Base64.NO_WRAP);
            String fileName = getFileName(uri);
            
            Message.Attachment attachment = new Message.Attachment(
                Message.AttachmentType.IMAGE, 
                base64Image, 
                fileName
            );
            currentAttachments.add(attachment);
            updateAttachmentPreview();
            
        } catch (Exception e) {
            Log.e(TAG, "Error reading image: ", e);
            Toast.makeText(this, R.string.file_read_error, Toast.LENGTH_SHORT).show();
        }
    }

    private void handleDocumentSelection(Uri uri) {
        try {

            long fileSize = getFileSize(uri);
            if (fileSize > MAX_FILE_SIZE) {
                Toast.makeText(this, R.string.file_too_large, Toast.LENGTH_SHORT).show();
                return;
            }
            

            InputStream inputStream = getContentResolver().openInputStream(uri);
            if (inputStream == null) {
                Toast.makeText(this, R.string.file_read_error, Toast.LENGTH_SHORT).show();
                return;
            }
            
            BufferedReader reader = new BufferedReader(new InputStreamReader(inputStream));
            StringBuilder content = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                content.append(line).append("\n");
            }
            reader.close();
            
            String base64Content = Base64.encodeToString(content.toString().getBytes(), Base64.NO_WRAP);
            String fileName = getFileName(uri);
            
            Message.Attachment attachment = new Message.Attachment(
                Message.AttachmentType.TEXT_FILE, 
                base64Content, 
                fileName
            );
            currentAttachments.add(attachment);
            updateAttachmentPreview();
            
        } catch (Exception e) {
            Log.e(TAG, "Error reading document: ", e);
            Toast.makeText(this, R.string.file_read_error, Toast.LENGTH_SHORT).show();
        }
    }

    private long getFileSize(Uri uri) {
        Cursor cursor = getContentResolver().query(uri, null, null, null, null);
        if (cursor != null && cursor.moveToFirst()) {
            int sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE);
            if (sizeIndex >= 0) {
                long size = cursor.getLong(sizeIndex);
                cursor.close();
                return size;
            }
            cursor.close();
        }
        return 0;
    }

    private String getFileName(Uri uri) {
        String fileName = "unknown";
        Cursor cursor = getContentResolver().query(uri, null, null, null, null);
        if (cursor != null && cursor.moveToFirst()) {
            int nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
            if (nameIndex >= 0) {
                fileName = cursor.getString(nameIndex);
            }
            cursor.close();
        }
        return fileName;
    }

    private void updateAttachmentPreview() {
        int imageCount = 0;
        int docCount = 0;
        
        for (Message.Attachment attachment : currentAttachments) {
            if (attachment.getType() == Message.AttachmentType.IMAGE) {
                imageCount++;
            } else {
                docCount++;
            }
        }
        

        if (imageCount > 0 || docCount > 0) {
            attachmentPreviewContainer.setVisibility(View.VISIBLE);
        } else {
            attachmentPreviewContainer.setVisibility(View.GONE);
        }
        
        if (imageCount > 0) {
            imageAttachmentIndicator.setVisibility(View.VISIBLE);
            imageCountText.setText(String.valueOf(imageCount));
        } else {
            imageAttachmentIndicator.setVisibility(View.GONE);
        }
        
        if (docCount > 0) {
            docAttachmentIndicator.setVisibility(View.VISIBLE);
            docCountText.setText(String.valueOf(docCount));
        } else {
            docAttachmentIndicator.setVisibility(View.GONE);
        }
    }

    private void clearImageAttachments() {
        currentAttachments.removeIf(a -> a.getType() == Message.AttachmentType.IMAGE);
        updateAttachmentPreview();
    }

    private void clearDocAttachments() {
        currentAttachments.removeIf(a -> a.getType() == Message.AttachmentType.TEXT_FILE);
        updateAttachmentPreview();
    }

    private void clearAllAttachments() {
        currentAttachments.clear();
        updateAttachmentPreview();
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


        boolean hasImageAttachment = userMessage.hasAttachments() && userMessage.getImageCount() > 0;
        
        if (hasImageAttachment) {

            JsonArray placeholderMessages = new JsonArray();
            JsonObject placeholderMsg = new JsonObject();
            placeholderMsg.addProperty("role", "user");
            placeholderMsg.addProperty("content", "placeholder");
            placeholderMessages.add(placeholderMsg);
            jsonBody.add("messages", placeholderMessages);
            

            JsonArray customMessages = new JsonArray();
            
            JsonObject systemMessage = new JsonObject();
            systemMessage.addProperty("role", "system");
            systemMessage.addProperty("content", "You are a helpful assistant.");
            customMessages.add(systemMessage);
            

            JsonObject userMsg = new JsonObject();
            userMsg.addProperty("role", "user");
            
            JsonObject contentObj = new JsonObject();
            

            StringBuilder questionBuilder = new StringBuilder();
            if (userMessage.getText() != null && !userMessage.getText().isEmpty()) {
                questionBuilder.append(userMessage.getText());
            }
            

            for (Message.Attachment attachment : userMessage.getAttachments()) {
                if (attachment.getType() == Message.AttachmentType.TEXT_FILE) {
                    try {
                        String decodedContent = new String(Base64.decode(attachment.getBase64Content(), Base64.NO_WRAP));
                        questionBuilder.append("\n\n--- ").append(attachment.getFileName()).append(" ---\n");
                        questionBuilder.append(decodedContent);
                    } catch (Exception e) {
                        Log.e(TAG, "Error decoding text file: ", e);
                    }
                }
            }
            
            contentObj.addProperty("question", questionBuilder.toString());
            

            for (Message.Attachment attachment : userMessage.getAttachments()) {
                if (attachment.getType() == Message.AttachmentType.IMAGE) {
                    contentObj.addProperty("image", attachment.getBase64Content());
                    break; 
                }
            }
            
            userMsg.add("content", contentObj);
            customMessages.add(userMsg);
            

            jsonBody.add("messages", customMessages);
            
        } else {

            JsonArray messages = new JsonArray();
            JsonObject systemMessage = new JsonObject();
            systemMessage.addProperty("role", "system");
            systemMessage.addProperty("content", "You are a helpful assistant.");
            messages.add(systemMessage);

            JsonObject userMsg = new JsonObject();
            userMsg.addProperty("role", "user");
            

            StringBuilder contentBuilder = new StringBuilder();
            if (userMessage.getText() != null && !userMessage.getText().isEmpty()) {
                contentBuilder.append(userMessage.getText());
            }
            
            for (Message.Attachment attachment : userMessage.getAttachments()) {
                if (attachment.getType() == Message.AttachmentType.TEXT_FILE) {
                    try {
                        String decodedContent = new String(Base64.decode(attachment.getBase64Content(), Base64.NO_WRAP));
                        contentBuilder.append("\n\n--- ").append(attachment.getFileName()).append(" ---\n");
                        contentBuilder.append(decodedContent);
                    } catch (Exception e) {
                        Log.e(TAG, "Error decoding text file: ", e);
                    }
                }
            }
            
            userMsg.addProperty("content", contentBuilder.toString());
            messages.add(userMsg);

            jsonBody.add("messages", messages);
        }
        
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
                // End model reply; LiveData observer will re-enable UI.
                chatViewModel.endModelReply();
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
                    chatViewModel.endModelReply();
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
                                        chatViewModel.endModelReply();
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
                        chatViewModel.endModelReply();
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
                    chatViewModel.startModelReply();
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
