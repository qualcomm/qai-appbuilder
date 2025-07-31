## Environment setup

Although Flet supports macOs, Windows, Linux, it is recommended to setup the environment on Linux system and WSL (Windows Subsystem for Linux).

Here are the setup steps on WSL.

### **Install WSL**

- Please use a computer with **x64 architecture**. ARM64 architecture PCs are not supported.

- Execute the following command in Powershell as **Administrator**. (Ubuntu-22.04 is recommended to avoid network issues caused by the openssl version). The minimum requirement is Ubuntu-20.04:

  ```sh
  wsl --install -d Ubuntu-22.04
  ```

- During the installation process, enter the username and password you want to create.

- After successful installation, the file system of Ubuntu-20.04 is mapped to File Explorer, and you can directly access and modify files here.

  ![image-Ubuntu](resource\image-Ubuntu.png)

- For more details about WSL, please refer to [Install WSL | Microsoft Learn](https://learn.microsoft.com/en-us/windows/wsl/install) and[Set up a WSL development environment | Microsoft Learn](https://learn.microsoft.com/en-us/windows/wsl/setup/environment). If WSL installation fails, please refer to [Manual installation steps for older versions of WSL | Microsoft Learn](https://learn.microsoft.com/en-us/windows/wsl/install-manual) to fix the issue.

- **Enter the Ubuntu-22.04 System** via command in Powershell (Administrator is not required):

  ```sh
  wsl -d Ubuntu-22.04
  ```


### **Install Dependencies in WSL Ubuntu-22.04**

- Install the necessary packages and update certificates:

  ```sh
  sudo apt update && sudo apt upgrade
  sudo apt-get install -y build-essential zlib1g zlib1g-dev python3-pip net-tools libgstreamer1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-tools libmpv-dev libgtk-3-0 libgtk-3-dev libssl-dev libffi-dev libbz2-dev ca-certificates liblzma-dev build-essential libssl-dev zlib1g-dev libbz2-dev liblzma-dev libsqlite3-dev libreadline-dev libncurses5-dev libffi-dev libexpat1-dev tk-dev uuid-dev clang
  sudo update-ca-certificates
  ```

- **Install Python 3.12.10**

  - Download and extract Python 3.12.10:

    ```sh
    cd ~
    wget https://www.python.org/ftp/python/3.12.10/Python-3.12.10.tgz
    tar -xzvf Python-3.12.10.tgz
    cd Python-3.12.10/
    ```

  - Create the installation directory and configure:

    ```sh
    sudo mkdir /usr/local/python3.12.10
    ./configure --prefix=/usr/local/python3.12.10 --enable-optimizations
    make -j8
    sudo make altinstall
    ```

  - Create symbolic links and update alternatives:

    ```sh
    sudo ln -s /usr/local/python3.12.10/bin/python3.12 /usr/bin/python3.12.10
    sudo update-alternatives --install /usr/bin/python python /usr/bin/python3.12.10 1
    sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12.10 1
    
    #select the index of python3.12.10 if multiple versions of python are install
    update-alternatives --config python 
    update-alternatives --config python3
    
    #confirm the python3.12.10 is used
    python -V
    python3 -V
    ```

  

### **Install Flet in Virtual Environment**

- Create and activate the virtual environment:

  ```sh
  cd ~
  mkdir flet-app
  cd flet-app
  python3 -m venv .venv
  source .venv/bin/activate
  ```

- Install Flet:

  ```sh
  pip install flet
  flet --version #0.28.3
  ```

  The proven stable and buildable version of Flet is **0.28.3**, so it is recommended to use this version. If a different version is installed, you can switch by running:

  ```shell
  pip install flet==0.28.3
  ```

- Create a new flet App:

  ```
  flet create my-first-app
  ```

  The command will create the following directory structure:

  ```
  ├── README.md
  ├── pyproject.toml
  ├── src
  │   ├── assets
  │   │   └── icon.png
  │   └── main.py
  └── storage
      ├── data
      └── temp
  ```

- Run flet app as a desktop app:

  ```
  flet run my-first-app
  ```

  This command will run `main.py` located in the directory `my-first-app/`.

- **Build flet app for Android**

  - Reference: [Packaging app for Android | Flet](https://flet.dev/docs/publish/android)

  - Use the following command to build an Android APK from`my-first-app`:

    ```sh
    cd ~/flet-app/my-first-app
    flet build apk -vv
    ```

    In this step, Flet will automatically download and install Flutter, JDK, and Android SDK, and configure the relevant paths to `$PATH` automatically.

    If the build fails, try looking for solutions under [Build Issues](#Build Issues).

  - Once successfully built, the APK could be find in `~/flet-app/my-first-app/build/apk/`. And you can use `adb` to install the APK to your phone.

  - Run the command to exit the virtual environment if not need.

    ```shell
    deactivate
    ```

  

- **Build Issues**

  - `AttributeError: module 'platform' has no attribute 'android_ver'`

    Solution: Downgrade pip and package versions

    ```sh
    pip install pip==23.0.1 packaging==23.1
    ```

  - `AttributeError: module 'pkgutil' has no attribute 'ImpImporter'. Did you mean: 'zipimporter'?`

    Solution: Install compatible pip and upgrade setuptools package.

    ```sh
    pip uninstall setuptools pkg_resources pip -y
    python3 -m ensurepip --upgrade
    python3 -m pip install --upgrade setuptools
    ```

  - Failed to access Google

    - The error shows as below:

      ![image-NetworkError](resource\image-NetworkError.png)

    - Solution: Get the dependency from domestic mirror instead of Google. Please modify the files as below.

      ```diff
      @@ ~/flet-app/my-first-app/build/flutter/android/build.gradle
      allprojects {
          repositories {
      -        google()
      -        mavenCentral()
      +        maven { url 'https://maven.aliyun.com/repository/google' }
      +        maven { url 'https://maven.aliyun.com/repository/central' }
          }
      }
      
      @@ ~/flet-app/my-first-app/build/flutter/android/gradle/wrapper/gradle-wrapper.properties
      -distributionUrl=https\://services.gradle.org/distributions/gradle-8.7-bin.zip
      +distributionUrl=https\://mirrors.aliyun.com/gradle/distributions/v8.7.0/gradle-8.7-bin.zip
      ```

      Note: if the provided `distributionUrl` is not effective any more, please search for an effective one from Internet.

  
- **Build GenieFletUI Tips**
  1. GenieFletUI need use openai module. So, you need add openai dependency in file <your_project_path>\pyproject.toml.
    ```
    dependencies = [
      "flet==0.28.3",
      "openai"
    ]
    ```
  2. GenieFletUI need communicate with GenieAPIService (a background LLM service) through network. So, you need add network access in <your_project_path>\build\flutter\android\app\src\main\AndroidManifest.xml
    ```
    <!-- flet: permission   -->
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
    <uses-permission android:name="android.permission.ACCESS_WIFI_STATE" />
    <uses-permission android:name="android.permission.INTERNET" />   <!-- ✅ network access -->
    <!-- flet: end of permission   -->
    ......
    <!--  -->
    <application
        android:label="flet-chat"
        android:name="${applicationName}"
        android:enableOnBackInvokedCallback="true"
        android:icon="@mipmap/ic_launcher">
        android:usesCleartextTraffic="true"> <!-- ✅ HTTP access -->
        <!-- flet: meta-data  -->
        <meta-data android:name="io.flutter.embedding.android.EnableImpeller" android:value="false" />
        <!-- flet: end of meta-data  -->
    ```
  3. By default, Flet builds "fat" APK which includes binaries for both arm64-v8a (64 bits) and armeabi-v7a (32 bits) architectures. You can configure Flet to split fat APK into smaller APKs for each platform by using --split-per-abi option or by setting split_per_abi in <your_project_path>\pyproject.toml:
  ```
  [tool.flet.android]
  split_per_abi = true
  ```
