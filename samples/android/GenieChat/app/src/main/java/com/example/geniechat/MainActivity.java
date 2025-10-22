package com.example.geniechat;

import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.util.Log;
import android.view.Menu;
import android.view.MenuItem;
import android.widget.EditText;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.preference.PreferenceManager;
import androidx.recyclerview.widget.LinearLayoutManager;
import androidx.recyclerview.widget.RecyclerView;

import com.example.geniechat.adapter.MessageAdapter;
import com.example.geniechat.model.Message;
import com.google.android.material.floatingactionbutton.FloatingActionButton;
import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.ResponseBody;

public class MainActivity extends AppCompatActivity {

    private static final String TAG = "MainActivity";

    private RecyclerView recyclerView;
    private EditText editTextMessage;
    private FloatingActionButton buttonSend;

    private MessageAdapter messageAdapter;
    private List<Message> messageList = new ArrayList<>();
    private OkHttpClient client;
    private Gson gson;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        initViews();
        initRecyclerView();
        initHttpClient();

        addMessage("你好！有什么可以帮你的吗？", false);
    }

    private void initViews() {
        recyclerView = findViewById(R.id.recyclerView);
        editTextMessage = findViewById(R.id.editTextMessage);
        buttonSend = findViewById(R.id.buttonSend);
        setSupportActionBar(findViewById(R.id.toolbar));
        buttonSend.setOnClickListener(v -> sendMessage());
    }

    private void initRecyclerView() {
        messageAdapter = new MessageAdapter(messageList);
        LinearLayoutManager layoutManager = new LinearLayoutManager(this);
        layoutManager.setStackFromEnd(true);
        recyclerView.setLayoutManager(layoutManager);
        recyclerView.setAdapter(messageAdapter);
    }

    private void initHttpClient() {
        client = new OkHttpClient.Builder()
                // 对于流式响应，可以适当延长读取超时时间
                .readTimeout(60, TimeUnit.SECONDS)
                .build();
        gson = new Gson();
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.main_menu, menu);
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

        addMessage(messageText, true);
        editTextMessage.setText("");

        callChatApi(messageText);
    }

    private void addMessage(String text, boolean isSentByUser) {
        runOnUiThread(() -> {
            messageList.add(new Message(text, isSentByUser));
            messageAdapter.notifyItemInserted(messageList.size() - 1);
            recyclerView.scrollToPosition(messageList.size() - 1);
        });
    }

    private void appendToLastMessage(String chunk) {
        runOnUiThread(() -> {
            if (messageList.isEmpty()) {
                return;
            }
            int lastIndex = messageList.size() - 1;
            Message lastMessage = messageList.get(lastIndex);

            // 确保我们只追加到AI的回复上
            if (!lastMessage.isSentByUser()) {
                String currentText = lastMessage.getText();
                // 如果是初始的“...”占位符，则直接替换
                String newText = "...".equals(currentText) ? chunk : currentText + chunk;

                messageList.set(lastIndex, new Message(newText, false));
                messageAdapter.notifyItemChanged(lastIndex);
                recyclerView.scrollToPosition(lastIndex);
            }
        });
    }

    private void callChatApi(String userMessage) {
        // 先添加一个占位符，表示AI正在思考
        addMessage("...", false);

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
        jsonBody.addProperty("stream", true); // <<<--- 设置为流式

        JsonArray messages = new JsonArray();
        JsonObject systemMessage = new JsonObject();
        systemMessage.addProperty("role", "system");
        systemMessage.addProperty("content", "You are a helpful assistant.");
        messages.add(systemMessage);

        JsonObject userMsg = new JsonObject();
        userMsg.addProperty("role", "user");
        userMsg.addProperty("content", userMessage);
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
                appendToLastMessage("\n\n网络错误: " + e.getMessage());
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
                    appendToLastMessage("\n\n请求失败: " + response.code());
                    return;
                }

                try (ResponseBody responseBody = response.body()) {
                    if (responseBody == null) return;

                    // 使用 BufferedReader 逐行读取
                    try (BufferedReader reader = new BufferedReader(new InputStreamReader(responseBody.byteStream()))) {
                        String line;
                        while ((line = reader.readLine()) != null) {
                            if (line.startsWith("data:")) {
                                String jsonData = line.substring(5).trim();

                                if ("[DONE]".equals(jsonData)) {
                                    // 数据流结束
                                    break;
                                }

                                // 解析这个小的数据块
                                JsonObject chunkObject = gson.fromJson(jsonData, JsonObject.class);
                                JsonArray choices = chunkObject.getAsJsonArray("choices");
                                if (choices != null && !choices.isEmpty()) {
                                    JsonObject delta = choices.get(0).getAsJsonObject().getAsJsonObject("delta");
                                    if (delta != null && delta.has("content")) {
                                        String contentChunk = delta.get("content").getAsString();
                                        // 将内容块追加到最后一条消息
                                        appendToLastMessage(contentChunk);
                                    }
                                }
                            }
                        }
                    }

                } catch (Exception e) {
                    Log.e(TAG, "Response processing error: ", e);
                    appendToLastMessage("\n\n处理回复时出错。");
                }
            }
        });
    }
}