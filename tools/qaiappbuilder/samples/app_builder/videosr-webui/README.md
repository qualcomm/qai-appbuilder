# VideoSR WebUI – AI Video/Image Super-Resolution (NPU Accelerated)

## Features

- 🎬 **AI Video Super-Resolution** – Enhance video quality using Real-ESRGAN x4plus
- 🖼️ **Image Super-Resolution** – 4x upscaling for images
- ⚡ **NPU Acceleration** – Qualcomm Hexagon NPU hardware acceleration
- 📊 **Real-time Progress** – Live progress tracking with ETA
- 🔒 **Privacy** – All processing is local, no cloud uploads
- 🎨 **Optical Flow Interpolation** – Motion-compensated frame synthesis for video

## Project Structure

```
videosr-webui/
├── app.yaml             # App Builder manifest
├── requirements.txt     # Python dependencies
├── start.bat            # Windows launcher
├── start.sh             # Linux/macOS launcher
├── backend/
│   ├── __init__.py
│   ├── main.py          # FastAPI application + routes
│   ├── inference.py     # NPU model loading & tile-based inference
│   └── schemas.py       # Pydantic request/response models
└── frontend/
    ├── index.html       # Main page
    ├── app.js           # Frontend logic
    └── styles.css       # Styles (light/dark theme)
```

## Quick Start

### Prerequisites

- Python 3.10+ (ARM64 recommended for NPU)
- Install dependencies: `pip install -r requirements.txt`
- QAI AppBuilder runtime (for NPU acceleration)
- **Model binary** (see below)

### Model Setup (Required)

This app requires a Real-ESRGAN x4plus QNN context binary that is **not
bundled or auto-downloaded**. You must convert it using QAI ModelBuilder's
model-builder feature:

1. Open QAI ModelBuilder chat, switch to **model-builder** mode
2. Enter: `Convert Real-ESRGAN x4plus to QNN context binary, input size 480×640, fp16`
3. The Agent will download the ONNX model, convert to QNN, and generate the `.bin`
4. Place the output file as:
   ```
   models/real-esrgan-x4plus/real_esrgan_480x640_fp16.bin
   ```

Optional fallback (lower NPU memory requirement):
- Convert with input size `360×480` → place as `real_esrgan_360x480_fp16.bin`

Without the model binary, the app starts but super-resolution will be unavailable.

### Running

**Windows:**
```cmd
start.bat [port]
```

**Linux/macOS:**
```bash
./start.sh [port]
```

Default port: 1975. Access at `http://127.0.0.1:1975/`

### With App Builder Host

The app is designed to run under the QAI ModelBuilder App Builder host:
```yaml
entry:
  app_module: backend.main:app
  health_path: /health
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/api/health` | Detailed health |
| POST | `/api/upload` | Upload video/image |
| POST | `/api/process` | Start SR processing |
| GET | `/api/task/{id}` | Task status |
| POST | `/api/task/{id}/cancel` | Cancel task |
| GET | `/api/download/{id}` | Download result |
| GET | `/api/file-preview?path=` | Preview file |
| GET | `/outputs/{filename}` | Serve output file |

## NPU Configuration

See [NPU_SETUP.md](NPU_SETUP.md) for detailed NPU acceleration setup.

## Technology

- **Backend:** FastAPI + uvicorn
- **Inference:** QNN HTP (Hexagon Tensor Processor) via qai_appbuilder
- **Model:** Real-ESRGAN x4plus (480×640 tile, fp16)
- **Video:** Optical flow motion compensation + H.264 re-encoding via ffmpeg
- **Frontend:** Vanilla JS with comparison slider UI

## License

MIT License
