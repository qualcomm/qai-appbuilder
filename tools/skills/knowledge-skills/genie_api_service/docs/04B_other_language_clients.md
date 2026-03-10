# GenieAPIService 其他语言客户端

GenieAPIService 兼容 OpenAI API，因此可以使用任何支持 OpenAI API 的客户端库。

## JavaScript/Node.js

```javascript
const OpenAI = require('openai');

const client = new OpenAI({
    baseURL: 'http://127.0.0.1:8910/v1',
    apiKey: '123'
});

async function chat() {
    const response = await client.chat.completions.create({
        model: 'Qwen2.0-7B',
        messages: [
            { role: 'user', content: '你好' }
        ],
        stream: true
    });

    for await (const chunk of response) {
        if (chunk.choices[0]?.delta?.content) {
            process.stdout.write(chunk.choices[0].delta.content);
        }
    }
}

chat();
```

---

## Java

```java
import com.theokanning.openai.OpenAiService;
import com.theokanning.openai.completion.chat.*;

public class GenieClient {
    public static void main(String[] args) {
        OpenAiService service = new OpenAiService("123", Duration.ofSeconds(30));
        service.setBaseUrl("http://127.0.0.1:8910/v1");

        ChatCompletionRequest request = ChatCompletionRequest.builder()
            .model("Qwen2.0-7B")
            .messages(Arrays.asList(
                new ChatMessage("user", "你好")
            ))
            .build();

        service.createChatCompletion(request)
            .getChoices()
            .forEach(choice -> System.out.println(choice.getMessage().getContent()));
    }
}
```

---

## Go

```go
package main

import (
    "context"
    "fmt"
    openai "github.com/sashabaranov/go-openai"
)

func main() {
    config := openai.DefaultConfig("123")
    config.BaseURL = "http://127.0.0.1:8910/v1"
    client := openai.NewClientWithConfig(config)

    resp, err := client.CreateChatCompletion(
        context.Background(),
        openai.ChatCompletionRequest{
            Model: "Qwen2.0-7B",
            Messages: []openai.ChatCompletionMessage{
                {
                    Role:    openai.ChatMessageRoleUser,
                    Content: "你好",
                },
            },
        },
    )

    if err != nil {
        fmt.Printf("Error: %v\n", err)
        return
    }

    fmt.Println(resp.Choices[0].Message.Content)
}
```

---

## C#

```csharp
using OpenAI;
using OpenAI.Chat;

var client = new OpenAIClient("123", new OpenAIClientOptions
{
    Endpoint = new Uri("http://127.0.0.1:8910/v1")
});

var chatClient = client.GetChatClient("Qwen2.0-7B");

var response = await chatClient.CompleteChatAsync(
    new ChatMessage[]
    {
        new SystemChatMessage("You are a helpful assistant."),
        new UserChatMessage("你好")
    }
);

Console.WriteLine(response.Value.Content[0].Text);
```

---

## Ruby

```ruby
require 'openai'

client = OpenAI::Client.new(
  access_token: "123",
  uri_base: "http://127.0.0.1:8910/v1"
)

response = client.chat(
  parameters: {
    model: "Qwen2.0-7B",
    messages: [
      { role: "user", content: "你好" }
    ]
  }
)

puts response.dig("choices", 0, "message", "content")
```

---

## PHP

```php
<?php
require 'vendor/autoload.php';

use OpenAI;

$client = OpenAI::factory()
    ->withApiKey('123')
    ->withBaseUri('http://127.0.0.1:8910/v1')
    ->make();

$response = $client->chat()->create([
    'model' => 'Qwen2.0-7B',
    'messages' => [
        ['role' => 'user', 'content' => '你好'],
    ],
]);

echo $response->choices[0]->message->content;
?>
```

---

## Rust

```rust
use reqwest;
use serde_json::json;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = reqwest::Client::new();
    
    let response = client
        .post("http://127.0.0.1:8910/v1/chat/completions")
        .json(&json!({
            "model": "Qwen2.0-7B",
            "messages": [
                {"role": "user", "content": "你好"}
            ]
        }))
        .send()
        .await?
        .json::<serde_json::Value>()
        .await?;
    
    println!("{}", response["choices"][0]["message"]["content"]);
    Ok(())
}

GitHub: https://github.com/quic/ai-engine-direct-helper