//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.example.genieapiservice;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.graphics.drawable.Icon;
import android.net.Uri;
import android.os.Binder;
import android.os.IBinder;
import android.system.Os;
import android.util.Log;

import androidx.core.app.NotificationCompat;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Scanner;




class MyNativeLib {
    public native void runService(String[] args);
    public native void stopService();
}



public class ForegroundService extends Service {
    private final static String TAG = "ForegroundService";
    private String modelRoot;
    private static final String DEFAULT_MODEL_NAME = "llm_llama3.1";
    static {
        try {
            // Ensure ADSP_LIBRARY_PATH is set before loading native libs.
            Os.setenv("ADSP_LIBRARY_PATH", "/data/local/tmp/qnn;/vendor/lib/rfsa/adsp", true);
        } catch (Exception e) {
            e.printStackTrace();
        }
        System.loadLibrary("JNIGenieAPIService");
        System.loadLibrary("GenieAPIService");
    }
    private final String mNotificationTitle = "Genie API Service";
    private static NotificationManager mNotificationManager = null;
    private static NotificationCompat.Builder mNotificationBuilder = null;
    public static boolean ServiceIsRunning = false;
    private String mLogDirectory;
    private int mLogLevelIndex = -1;
    private final IBinder binder = new LocalBinder();
    private MyNativeLib nativeLib;

    public class LocalBinder extends Binder {
        ForegroundService getService() {
            return ForegroundService.this;
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return binder;
    }

    @Override
    public boolean onUnbind(Intent intent) {
        return super.onUnbind(intent);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        modelRoot = getApplicationContext().getFilesDir().getAbsolutePath() + "/GenieModels";
        if (LogUtils.LOG_DIRECTORY.isEmpty()) {
            LogUtils.LOG_DIRECTORY = getApplicationContext().getFilesDir().getAbsolutePath() + File.separator + "Logs";
        }
        try {
            boolean isAutomotive =
                    getPackageManager().hasSystemFeature(
                            PackageManager.FEATURE_AUTOMOTIVE);
            if (isAutomotive) {
                String appNativeDir = getApplicationContext().getApplicationInfo().nativeLibraryDir;
                QnnLibCopier.copyIfNeeded(getApplicationContext());
                Os.setenv("ADSP_LIBRARY_PATH", "/data/local/tmp/qnn;/vendor/lib/rfsa/adsp", true);
                Os.setenv("LD_LIBRARY_PATH", appNativeDir, true);
                LogUtils.logDebug(TAG, "ADSP_LIBRARY_PATH=" + System.getenv("ADSP_LIBRARY_PATH"), LogUtils.LOG_DEBUG);
                LogUtils.logDebug(TAG, "LD_LIBRARY_PATH=" + System.getenv("LD_LIBRARY_PATH"), LogUtils.LOG_DEBUG);
            } else {
                String nativeLibPath = getApplicationContext().getApplicationInfo().nativeLibraryDir;
                Os.setenv("ADSP_LIBRARY_PATH", nativeLibPath, true);
                Os.setenv("LD_LIBRARY_PATH", nativeLibPath, true);
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        mLogDirectory = LogUtils.LOG_DIRECTORY ;
        File logFile = new File(mLogDirectory);
        if (!logFile.exists()) {
            logFile.mkdir();
        }
        SharedPreferences sharedPreferences = getSharedPreferences("LogLevel",Context.MODE_PRIVATE);
        mLogLevelIndex = sharedPreferences.getInt("level",2);
        //new RecordSystemLogcatTask().start();
        LogUtils.logDebug(TAG,"onCreate called ",LogUtils.LOG_DEBUG);
    }

    private String resolveModelRoot() {
        String internal = getApplicationContext().getFilesDir().getAbsolutePath() + "/GenieModels";
        String[] candidates = {
                internal,
                "/storage/emulated/10/GenieModels",
                "/sdcard/GenieModels",
                "/data/media/10/GenieModels",
                "/data/local/tmp"
        };
        for (String candidate : candidates) {
            String first = getFirstModel(candidate);
            if (first != null) {
                LogUtils.logDebug(TAG, "Using model root: " + candidate, LogUtils.LOG_DEBUG);
                return candidate;
            }
            File cfg = new File(candidate + "/" + DEFAULT_MODEL_NAME + "/config.json");
            if (cfg.exists()) {
                LogUtils.logDebug(TAG, "Using model root (default model present): " + candidate, LogUtils.LOG_DEBUG);
                return candidate;
            }
        }
        LogUtils.logDebug(TAG, "No valid model root found. Checked: " + String.join(", ", candidates), LogUtils.LOG_ERROR);
        return internal;
    }

    private void stopServiceSession() {
        new Thread(() -> {
            nativeLib.stopService();
        }).start();

    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        LogUtils.logDebug(TAG,"onDestroy called ",LogUtils.LOG_DEBUG);
        stopServiceSession();
        ServiceIsRunning = false;
        stopForeground(true);
    }

    public static void updateNotification(String value) {
        if (mNotificationBuilder != null && mNotificationManager != null) {
            LogUtils.logDebug(TAG,"the notification :  " + value,LogUtils.LOG_DEBUG);
            NotificationCompat.BigTextStyle bigTextStyle = new NotificationCompat.BigTextStyle();
            bigTextStyle.bigText(value);
            mNotificationBuilder.setStyle(bigTextStyle);
            mNotificationManager.notify(1, mNotificationBuilder.build());
        }
    }

    private String getFirstModel(String fileDir) {
        LogUtils.logDebug(TAG,"GetModelFileList :  " + fileDir ,LogUtils.LOG_ERROR);
        File file = new File(fileDir);
        File[] subFile = file.listFiles();
        if (subFile == null) {
            LogUtils.logDebug(TAG,"model list is null ",LogUtils.LOG_ERROR);
            return null;
        }
        for(int iFileLength = 0; iFileLength < subFile.length; iFileLength++) {
            if (subFile[iFileLength].isDirectory()) {
                String filename = subFile[iFileLength].getName();
                if (isValidModel(filename, fileDir)) {
                    return filename;
                }
            }
        }
        return null;
    }

    private boolean isValidModel(String dirName, String modelRoot) {
        LogUtils.logDebug(TAG, "isValidModel : " + dirName, LogUtils.LOG_DEBUG);
        String configFile = modelRoot + "/" + dirName + "/config.json";
        File file = new File(configFile);
        if (file.exists()) {
            return true;
        }
        return false;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // Start Service
        LogUtils.logDebug(TAG,"onStartCommand begin = " + intent,LogUtils.LOG_DEBUG);
        new Thread(() -> {
            int newFileIndex = getLogFileIndex();
            String logFileName = mLogDirectory + File.separator
                    + "Log:" + String.valueOf(newFileIndex);
            LogUtils.logDebug(TAG,"logFileName = " + logFileName + " mLogLevelIndex = " + mLogLevelIndex,LogUtils.LOG_DEBUG);
            nativeLib = new MyNativeLib();
            modelRoot = resolveModelRoot();
            String currentModel = getFirstModel(modelRoot);
            String configFile = null;
            if (currentModel != null) {
                configFile = modelRoot + "/" + currentModel + "/config.json";
            } else {
                configFile = modelRoot + "/" + DEFAULT_MODEL_NAME + "/config.json";
            }
            LogUtils.logDebug(TAG,"config file = " + configFile,LogUtils.LOG_DEBUG);
            File cfg = new File(configFile);
            LogUtils.logDebug(TAG,"config exists=" + cfg.exists() + " size=" + (cfg.exists() ? cfg.length() : -1),LogUtils.LOG_DEBUG);
            File prompt = new File(modelRoot + "/" + (currentModel != null ? currentModel : DEFAULT_MODEL_NAME) + "/prompt.json");
            LogUtils.logDebug(TAG,"prompt exists=" + prompt.exists() + " path=" + prompt.getAbsolutePath(),LogUtils.LOG_DEBUG);
            File modelDir = new File(modelRoot + "/" + (currentModel != null ? currentModel : DEFAULT_MODEL_NAME) + "/models");
            LogUtils.logDebug(TAG,"model dir exists=" + modelDir.exists() + " path=" + modelDir.getAbsolutePath(),LogUtils.LOG_DEBUG);
            LogUtils.logDebug(TAG,"cinfig file = " + configFile,LogUtils.LOG_DEBUG);
            if (!cfg.exists()) {
                LogUtils.logDebug(TAG, "No valid config.json found; aborting service start.", LogUtils.LOG_ERROR);
                return;
            }
            String[] commandArgs = {"main", "-c", configFile, "-l", "-d", mLogLevelIndex != -1 ? String.valueOf(mLogLevelIndex) : "2", "-f", logFileName};
            nativeLib.runService(commandArgs);
            System.out.println("after runService");
        }).start();
        // Connect to Service.
        new Thread(() -> {
            while (true) {
                try {
                    Thread.sleep(1000);  // Sleep 1 second.
                    URL url = new URL("http://localhost:8910/");
                    HttpURLConnection connection = (HttpURLConnection) url.openConnection();
                    connection.setRequestMethod("GET");
                    int responseCode = connection.getResponseCode();
                    System.out.println("in mainactivity, responseCode: " +responseCode);
                    if (responseCode == HttpURLConnection.HTTP_OK) {
                        Scanner scanner = new Scanner(connection.getInputStream());
                        StringBuilder response = new StringBuilder();
                        while (scanner.hasNextLine()) {
                            response.append(scanner.nextLine());
                        }
                        scanner.close();
                        System.out.println("Response: " + response.toString());
                        //MainActivity.hasGotServiceMsg = true;
                        MainActivity.service_msg = response.toString();
                        break;
                    }
                } catch (IOException | InterruptedException e) {
                    e.printStackTrace();
                }
            }
        }).start();

        mNotificationManager = (NotificationManager)
                getSystemService(Context.NOTIFICATION_SERVICE);
        NotificationChannel channel = new NotificationChannel("genie_channel_id", "Genie API Service",
                NotificationManager.IMPORTANCE_LOW);
        mNotificationManager.createNotificationChannel(channel);

        Intent NotificationIntent = new Intent(this, MainActivity.class);
        PendingIntent pendingIntent = PendingIntent.getActivity(this, 0, NotificationIntent, PendingIntent.FLAG_IMMUTABLE);
        mNotificationBuilder = new NotificationCompat.Builder(this, "genie_channel_id");
        mNotificationBuilder.setDeleteIntent(null)
                .setContentIntent(pendingIntent)
                .setContentText(mNotificationTitle + " is running")
                .setOngoing(true)
                .setSmallIcon(R.drawable.ic_launcher_qai)
                .build();
        startForeground(1, mNotificationBuilder.build(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        ServiceIsRunning = true;
        flags += Service.START_FLAG_REDELIVERY | Service.START_FLAG_RETRY;
        LogUtils.logDebug(TAG,"onStartCommand end = " + intent,LogUtils.LOG_DEBUG);
        return super.onStartCommand(intent, flags, startId);
    }


    private int getLogFileIndex() {
        if (mLogDirectory == null) {
            mLogDirectory = LogUtils.LOG_DIRECTORY;
        }
        File logDir = new File(mLogDirectory);
        if (!logDir.exists()) {
            logDir.mkdir();
        }
        File[] files = logDir.listFiles();
        int index = 0;
        LogUtils.logDebug(TAG,"the file number : " + files.length,LogUtils.LOG_DEBUG);
        for (int i = files.length-1; i >=0; i--) {
            String name = files[i].getName();
            if (name.length() <= 4 || !name.startsWith("Log:")) {
                continue;
            }
            index = Integer.valueOf(name.substring(4)).intValue();
            if (index >= 9) {
                files[i].delete();
                continue;
            }
            String newName = files[i].getName().substring(0,4) + String.valueOf(index+1);
            File newFile = new File(mLogDirectory + File.separator + newName);
            LogUtils.logDebug(TAG,"the file : " + files[i].getName() + " size : " + files[i].length()
                    + " rename to : " + newName + " size : " + newFile.length(),LogUtils.LOG_DEBUG);
            try {
                if (!files[i].renameTo(newFile)) {
                    LogUtils.logDebug(TAG,files[i].getName() + "rename to " + newFile.getName() + " failed.",LogUtils.LOG_ERROR);
                } else {
                    LogUtils.logDebug(TAG,"the file : " + files[i].getName() + " size : " + files[i].length()
                            + " rename to : " + newName + " size : " + newFile.length(),LogUtils.LOG_DEBUG);
                    files[i].delete();
                }
            } catch (Exception exception) {
                exception.printStackTrace();
            }
        }
        return 1;
    }


    public class RecordSystemLogcatTask extends Thread {

        private final static String TAG = "RecordSystemLogcatTask";
        private Process mProcess = null;
        private OutputStreamWriter mSystemInfoWriter = null;
        private final int mFileMaxSize = 10*1024*1024;
        private String mLogFileName;

        public RecordSystemLogcatTask() {
            if (mSystemInfoWriter == null) {
                try {
                    int newFileIndex = getLogFileIndex();
                    mLogFileName = mLogDirectory + File.separator
                             + "Log:" + String.valueOf(newFileIndex);
                    mSystemInfoWriter = new OutputStreamWriter(new FileOutputStream(mLogFileName));
                } catch (FileNotFoundException e) {
                    e.printStackTrace();
                }
            }
        }


        @Override
        public void run() {
            BufferedReader bufferedReader = null;
            try {
                mProcess = Runtime.getRuntime().exec("logcat -c");
                mProcess.destroy();
                String appPid = String.valueOf(android.os.Process.myPid());
                mProcess = Runtime.getRuntime().exec("logcat -b all | grep " + appPid);
                InputStreamReader inputStreamReader = new InputStreamReader(mProcess.getInputStream());
                bufferedReader = new BufferedReader(inputStreamReader);
                String line = null;
                while ((line = bufferedReader.readLine()) != null) {
                    int index = 0;
                    if (line.contains("genieapiservice_log")) {
                        index = line.indexOf("genieapiservice_log") + 19;
                    } else if (line.contains("genieapiservice_log(genie)")) {
                        index = line.indexOf("genieapiservice_log") + 26;
                    } else {
                        continue;
                    }
                    mSystemInfoWriter.write(line.substring(index));
                    mSystemInfoWriter.write("\n");
                    mSystemInfoWriter.flush();
                    File logFile = new File(mLogFileName);
                    if (logFile.length() >= mFileMaxSize) {
                        try {
                            mSystemInfoWriter.close();
                        } catch (IOException ioException) {
                            ioException.printStackTrace();
                        }
                        int newFileIndex = getLogFileIndex();
                        mLogFileName = mLogDirectory + File.separator
                                + "Log:" + String.valueOf(newFileIndex);
                        mSystemInfoWriter = new OutputStreamWriter(new FileOutputStream(mLogFileName));
                    }
                }
            } catch(IOException e) {
                e.printStackTrace();
            } finally {
                try {
                    mSystemInfoWriter.close();
                    if (bufferedReader != null) {
                        bufferedReader.close();
                    }
                } catch (IOException ioException) {
                    ioException.printStackTrace();
                }
                mProcess.destroy();
            }
        }
    }

}