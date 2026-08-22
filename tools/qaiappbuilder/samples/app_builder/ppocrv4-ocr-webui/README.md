# PP-OCRv4 OCR WebUI

A standalone App Builder WebUI for the `ppocrv4` model pack. It uses a FastAPI backend and a pure HTML/CSS/JS frontend. Inference runs in-process on the local device through `qai_appbuilder`; the frontend calls this app's own `/api/infer` endpoint.

## Run

Preferred: open the QAI ModelBuilder host UI and use the **Apps / 应用** menu to run, preview, stop, or package this app. The host allocates a port, waits for `/health`, then opens the browser.

Manual run is also available:

```bat
run.bat
```

Then open `http://127.0.0.1:8000/`.

## Required model

- Model id: `ppocrv4`
- Pack directory: `${APP_ROOT}/factory/chat_features/app-builder/models/ppocrv4`
- Standard weights directory: `${APP_ROOT}/models/ppocrv4`
- Optional packaged-app weights directory: `<package>/models/ppocrv4`
- Required context binaries: `det.bin`, `cls.bin`, `rec_320.bin`, `rec_640.bin`, `rec_960.bin`

Model context binaries are large runtime assets and are not source code. They are not committed with this WebUI. If model loading fails, install/copy the required `.bin` files to `${APP_ROOT}/models/ppocrv4` or package them with the app.

## Input and output

Input controls:

- Image upload: PNG/JPG/JPEG/WEBP, up to 10 MB
- Language hint: `auto`, `zh`, or `en`
- Auto-rotate upside-down text lines
- Detection threshold: lower values detect more faint text
- Recognition confidence threshold: higher values filter weak reads

Output:

- Full OCR text in reading order
- Detected line table with `line_idx`, `text`, and `conf`
- Overlay image with detected quadrilateral boxes
- JSON response fields: `lines`, `fullText`, `lang_detected`, `page_size`, `metrics`

## Known limitations

PP-OCRv4 is mainly for printed Chinese/English text. Handwriting, severe blur, extreme rotation, tiny characters, stylized fonts, and non-zh/en languages can be unreliable. Reading order is heuristic; multi-column documents and tables may need layout-specific postprocessing.

## Packaging note

The produced package is not a fully offline bundle. The target machine still needs a QAI ModelBuilder Python environment with `qai_appbuilder` and the QNN runtime. The zip does not bundle Python or the QNN SDK.
