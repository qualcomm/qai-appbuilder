# model_input_builder 文件级说明

> 本文件是 `model_input_builder.h`（及其 `ModelInputBuilder` 实现）的就近说明，只承载与该文件强绑定的易错点。

## 易错点：区分两种多模态请求风格

- **OpenAI 标准** `content=[{type:image_url|input_audio,...}]` 走 `ModelInputBuilder::ProcessArray`。
- **GenieAPIClient 风格**的 object（`question`/`image`/`audio` 平铺字段）走 `ProcessObject`。
- 两条路径互不影响，改一个不会自动覆盖另一个。
