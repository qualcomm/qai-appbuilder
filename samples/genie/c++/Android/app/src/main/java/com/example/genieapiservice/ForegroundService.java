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
import android.content.pm.ServiceInfo;
import android.graphics.drawable.Icon;
import android.net.Uri;
import android.os.Binder;
import android.os.Build;
import android.os.Environment;
import android.os.FileUtils;
import android.os.IBinder;
import android.os.Message;
import android.provider.Settings;
import android.system.Os;
import android.util.Log;

import androidx.core.app.NotificationCompat;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
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
    static {
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
        if (LogUtils.LOG_DIRECTORY.isEmpty()) {
            LogUtils.LOG_DIRECTORY = getApplicationContext().getFilesDir().getAbsolutePath() + File.separator + "Logs";
        }
        try {
            String nativeLibPath = getApplicationContext().getApplicationInfo().nativeLibraryDir;
            Os.setenv("ADSP_LIBRARY_PATH", nativeLibPath, true);
            Os.setenv("LD_LIBRARY_PATH", nativeLibPath, true);
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
                if (isValidModel(filename)) {
                    return filename;
                }
            }
        }
        return null;
    }

    private boolean isValidModel(String dirName) {
        LogUtils.logDebug(TAG, "isValidModel : " + dirName, LogUtils.LOG_DEBUG);
        String configFile = "/sdcard/GenieModels/" + dirName + "/config.json";
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
            String currentModel = getFirstModel("/sdcard/GenieModels");
            String configFile = null;
            //LogUtils.logDebug(TAG,"onStartCommand in child thread = " + intent.getStringExtra("modelName"),LogUtils.LOG_DEBUG);
            if (currentModel != null) {
                configFile = "/sdcard/GenieModels/" + currentModel + "/config.json";
            } else {
                configFile ="/sdcard/GenieModels/qwen2.0_7b-ssd/config.json";
            }
            LogUtils.logDebug(TAG,"cinfig file = " + configFile,LogUtils.LOG_DEBUG);
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
            index = Integer.valueOf(files[i].getName().substring(4)).intValue();
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