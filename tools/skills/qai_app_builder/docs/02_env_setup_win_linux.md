# 环境准备 (Windows & Linux)

## 2. 环境准备

### 2.1 Windows 环境配置

#### 步骤 1：安装依赖软件

**1. 安装 Git**

```bash
# 下载 Git for ARM64
https://github.com/dennisameling/git/releases/download/v2.47.0.windows.2/Git-2.47.0.2-arm64.exe
```

**2. 安装 Python 3.12.8**

使用 **x64 版本** 可获得更好的 Python 扩展生态(当前相对来说，有更多的 Python 扩展可在x64 Python 环境中运行，而对于 ARM64 Python，有部分扩展需要自己编译)：

```bash
# 下载 Python 3.12.8 x64
https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
```

或使用 **ARM64 版本** 以获得更好的性能：

```bash
# 下载 Python 3.12.8 ARM64
https://www.python.org/ftp/python/3.12.8/python-3.12.8-arm64.exe
```

⚠️ **重要提示**：

- 安装时必须勾选 "Add python.exe to PATH"
- 如果系统中有多个 Python 版本，确保新安装的版本在 PATH 环境变量的首位

验证 Python 版本顺序：

```cmd
where python
```

**3. 安装 Visual C++ Redistributable**

```bash
# 下载并安装
https://aka.ms/vs/17/release/vc_redist.x64.exe
https://aka.ms/vs/17/release/vc_redist.arm64.exe
```

#### 步骤 2：克隆 QAI AppBuilder 仓库

```bash
# 克隆仓库
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive

# 如果已克隆，更新代码
cd ai-engine-direct-helper
git pull --recurse-submodules
```

#### 步骤 3：安装 QAI AppBuilder Python 扩展

从 [GitHub Release](https://github.com/quic/ai-engine-direct-helper/releases) 下载对应版本的 `.whl` 文件：

```bash
# 对于 x64 Python
pip install qai_appbuilder-{version}-cp312-cp312-win_amd64.whl

# 对于 ARM64 Python
pip install qai_appbuilder-{version}-cp312-cp312-win_arm64.whl
```

💡 **重要提示**：从 v2.0.0 版本开始，QAI AppBuilder Python 扩展已经包含了所有必需的依赖库（包括 Qualcomm® AI Runtime SDK 运行时库），无需额外安装 Qualcomm® AI Runtime SDK。这大大简化了 Python 开发者的环境配置过程。

### 2.2 Linux 环境配置

#### 步骤 1：安装依赖软件

**1. 安装基础工具**

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y git python3 python3-pip build-essential

# 验证安装
python3 --version
pip3 --version
```

**2. 安装 Python 依赖**

```bash
# 安装常用的 Python 库
pip3 install numpy pillow opencv-python
```

#### 步骤 2：克隆 QAI AppBuilder 仓库

```bash
# 克隆仓库
git clone https://github.com/quic/ai-engine-direct-helper.git --recursive

# 如果已克隆，更新代码
cd ai-engine-direct-helper
git pull --recurse-submodules
```

#### 步骤 3：安装 QAI AppBuilder Python 扩展

从 [GitHub Release](https://github.com/quic/ai-engine-direct-helper/releases) 下载对应版本的 `.whl` 文件：

```bash
# 对于 Linux ARM64
pip3 install qai_appbuilder-{version}-cp310-cp310-linux_aarch64.whl
```

💡 **重要提示**：从 v2.0.0 版本开始，QAI AppBuilder Python 扩展已经包含了所有必需的依赖库（包括 Qualcomm® AI Runtime SDK 运行时库），无需额外安装 Qualcomm® AI Runtime SDK。

#### 步骤 4：配置环境变量（可选）

对于某些 Linux 平台，可能需要设置 `ADSP_LIBRARY_PATH` 环境变量：

```bash
# 添加到 ~/.bashrc 或 ~/.zshrc
export ADSP_LIBRARY_PATH=/path/to/qnn/libs

# 使配置生效
source ~/.bashrc
```

#### Linux 与 Windows 的主要区别

| 项目      | Windows                          | Linux                                  |
| ------- | -------------------------------- | -------------------------------------- |
| 库文件扩展名  | `.dll`                           | `.so`                                  |
| 后端库     | `QnnHtp.dll`, `QnnCpu.dll`       | `libQnnHtp.so`, `libQnnCpu.so`         |
| 系统库     | `QnnSystem.dll`                  | `libQnnSystem.so`                      |
| Python  | `python.exe`                     | `python3`                              |
| 路径分隔符   | `\` (反斜杠)                        | `/` (正斜杠)                              |
| 环境变量设置  | 系统属性 → 环境变量                     | `~/.bashrc` 或 `~/.zshrc`               |
| 特殊环境变量  | 无                                | `ADSP_LIBRARY_PATH` (某些平台需要)           |
