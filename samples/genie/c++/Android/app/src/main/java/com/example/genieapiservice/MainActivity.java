package com.example.genieapiservice;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;

import android.app.ActivityManager;
import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.ServiceConnection;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.IBinder;
import android.os.Process;
import android.provider.Settings;
import android.util.Log;
import android.view.Menu;
import android.view.MenuItem;
import android.view.SubMenu;
import android.view.View;
import android.webkit.WebView;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import android.system.Os;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Scanner;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.os.Message;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.widget.Toast;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.Enumeration;
import com.example.genieapiservice.databinding.ActivityMainBinding;
import java.io.BufferedReader;
import java.io.FileReader;

class IPAddressUtils {
    public static String getIPAddress(Context context) {
        ConnectivityManager cm = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
        NetworkInfo activeNetwork = cm.getActiveNetworkInfo();
        if (activeNetwork != null) {
            if (activeNetwork.getType() == ConnectivityManager.TYPE_WIFI) {
                return getWifiIPAddress(context);
            } else if (activeNetwork.getType() == ConnectivityManager.TYPE_MOBILE) {
                return getMobileIPAddress();
            }
        }
        return null;
    }

    private static String getWifiIPAddress(Context context) {
        WifiManager wifiManager = (WifiManager) context.getSystemService(Context.WIFI_SERVICE);
        WifiInfo wifiInfo = wifiManager.getConnectionInfo();
        int ipInt = wifiInfo.getIpAddress();
        return String.format("%d.%d.%d.%d", (ipInt & 0xff), (ipInt >> 8 & 0xff), (ipInt >> 16 & 0xff), (ipInt >> 24 & 0xff));
    }

    private static String getMobileIPAddress() {
        try {
            Enumeration<NetworkInterface> interfaces = NetworkInterface.getNetworkInterfaces();
            while (interfaces.hasMoreElements()) {
                NetworkInterface networkInterface = interfaces.nextElement();
                if (networkInterface.isLoopback() ||!networkInterface.isUp()) {
                    continue;
                }

                Enumeration<InetAddress> addresses = networkInterface.getInetAddresses();
                while (addresses.hasMoreElements()) {
                    InetAddress address = addresses.nextElement();
                    if (!address.isLoopbackAddress() && address instanceof Inet4Address) {
                        return address.getHostAddress();
                    }
                }
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return null;
    }
}

public class MainActivity extends AppCompatActivity {

    private static final String TAG = "MainActivity";
    private ActivityMainBinding binding;
    private TextView memoryInfo;
    private TextView memoryInfoSub;
    private WebView webView;
    private TextView ipView;
    private TextView mTile;
    private Button mStartService;
    //private Button mStopService;
    private ClipboardManager mClipboard;
    public static String service_msg = "";
    public static boolean hasGotServiceMsg = false;
    private static final String stop_msg = "<!DOCTYPE html>    <html>    <head>    <meta charset=\"UTF-8\">    <title>Genie " +
            "OpenAI API Service</title>    <style>    body { word-wrap: break-word; white-space: normal; }    h1 {text-align: " +
            "center;}    </style>    </head>    <body>     <br><br>    <h1 style=\"font-size:1.5em;\">The service is not running. <br>Click the button above to launch it.</h1>    </body>    </html>";
    private final String[] logLevelItems = {"Error","Warning","Info","Debug","Verbose"};
    private static final String starting_msg = "<!DOCTYPE html>    <html>    <head>    <meta charset=\"UTF-8\">    <title>Genie " +
            "OpenAI API Service</title>    <style>    body { word-wrap: break-word; white-space: normal; }    h1 {text-align: " +
            "center;}    </style>    </head>    <body>     <br><br>    <h1>We're getting things ready for you...</h1>    </body>    </html>";

    private int mLogLevelIndex = -1;
    private Thread mUpdateMemThread;
    private boolean mExitThread;

    private Handler updateViewHandler = new Handler(Looper.getMainLooper()) {
        @Override
        public void handleMessage(Message msg) {
            if (msg.what == 1) {
                displayMemoryInfo();
                if (hasGotServiceMsg) {
                    hasGotServiceMsg = false;
                    updateWebView();
                }
            }
        }
    };


    private void updateWebView() {
        LogUtils.logDebug(TAG,"updateView: service_msg = " + service_msg,LogUtils.LOG_DEBUG);
        if (!service_msg.isEmpty()) {
            webView.loadData(service_msg, "text/html", "UTF-8");
        }
        if (mStartService.getVisibility() != View.VISIBLE) {
            mStartService.setVisibility(View.VISIBLE);
        }
        if (ForegroundService.ServiceIsRunning && !mStartService.isEnabled()) {
            mStartService.setEnabled(true);
        }
    }

    private void updateIpView() {
        String ipAddress = IPAddressUtils.getIPAddress(MainActivity.this);
        if (ipAddress != null) {
            ipView.setText("IP Address & Port: " + ipAddress + ":8910");
        } else {
            ipView.setText("Can't get IP Address.");
        }
    }

    private String Convert_kb_to_MB(String kbString){
        String mbString = "";
        String[] parts = kbString.split("\\s+");
        long kbValue = Long.parseLong(parts[1]);
        double mbValue = kbValue / 1024.0;

        mbString = parts[0] + " " + String.format("%.0f", mbValue) + "MB ";
        return mbString;
    }
    public void displayMemoryInfo(){
        String memTotal = null;
        String memFree = null;
        String memAvailable = null;
        String buffers = null;
        String cached = null;
        try (BufferedReader reader = new BufferedReader(new FileReader("/proc/meminfo"))) {
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.startsWith("MemTotal:")) {
                    memTotal = line;
                } else if (line.startsWith("MemFree:")) {
                    memFree = line;
                } else if (line.startsWith("MemAvailable:")) {
                    memAvailable = line;
                } else if (line.startsWith("Buffers:")) {
                    buffers = line;
                } else if (line.startsWith("Cached:")) {
                    cached = line;
                }

                if (memTotal != null && memFree != null && memAvailable != null && buffers != null && cached != null) {
                    break;
                }
            }
        } catch (IOException e) {
            System.out.println(e);
        }
        String memString = Convert_kb_to_MB(memTotal)  + "\n" + Convert_kb_to_MB(memAvailable);
        memoryInfo.setText(memString);
        String memSubString = Convert_kb_to_MB (memFree) + "\n"  + Convert_kb_to_MB (buffers) + "\n"  + Convert_kb_to_MB (cached);
        memoryInfoSub.setText(memSubString);
        if (ForegroundService.ServiceIsRunning) {
            String memInfo = "[ DON'T REMOVE THIS NOTIFICATION ]\nMem: " + Convert_kb_to_MB(memTotal.substring(3))
                    + Convert_kb_to_MB(memAvailable.substring(3)) + Convert_kb_to_MB (memFree.substring(3))
                    + Convert_kb_to_MB ("Buffer:"+buffers.substring(8)) + Convert_kb_to_MB (cached);
            ForegroundService.updateNotification(memInfo);
        }

    }

    /*private ServiceConnection connection = new ServiceConnection() {
        @Override
        public void onServiceConnected(ComponentName name, IBinder service) {
            ForegroundService.LocalBinder binder = (ForegroundService.LocalBinder) service;
            ForegroundService GenieService = binder.getService();
        }

        @Override
        public void onServiceDisconnected(ComponentName name) {

        }
    };*/

    private void startForegroundService() {
        Intent serviceIntent = new Intent(this, ForegroundService.class);
        startForegroundService(serviceIntent);
        //bindService(serviceIntent,connection,Context.BIND_AUTO_CREATE);
    }

    private void stopForegroundService() {
        //unbindService(connection);
        Intent serviceIntent = new Intent(this, ForegroundService.class);
        stopService(serviceIntent);
    }

    public void requestPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager()) {
                Intent intent = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION);
                intent.setData(Uri.parse("package:" + getPackageName()));
                intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(intent);
            }
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        binding = ActivityMainBinding.inflate(getLayoutInflater());
        setContentView(binding.getRoot());
        setSupportActionBar(binding.toolbar);
        mClipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        getSupportActionBar().setDisplayShowTitleEnabled(false);
        requestPermission();
        mExitThread = false;
        mTile = findViewById(R.id.title);
        mTile.setText("GenieAPIService");
        if (LogUtils.LOG_DIRECTORY.isEmpty()) {
            LogUtils.LOG_DIRECTORY = getApplicationContext().getFilesDir().getAbsolutePath() + File.separator + "Logs";
        }
        // update the meminfo view
        mUpdateMemThread = new Thread(new Runnable() {
            @Override
            public void run() {
                while (!mExitThread) {
                    try {
                        Thread.sleep(1000);  // Sleep 1 second.
                        Message message = new Message();
                        message.what = 1;
                        updateViewHandler.sendMessage(message);
                    } catch (InterruptedException e) {
                        e.printStackTrace();
                    }
                }
            }
        });
        mStartService = binding.startService;
        if (ForegroundService.ServiceIsRunning) {
            mStartService.setText("Stop Service");
        } else {
            mStartService.setText("Start Service");
        }
        mStartService.setVisibility(View.INVISIBLE);
        mStartService.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                if (ForegroundService.ServiceIsRunning) {
                    mStartService.setEnabled(false);
                    stopForegroundService();
                    mStartService.setText("Start Service");
                    service_msg="";
                    webView.loadData(stop_msg, "text/html", "UTF-8");
                    mStartService.setEnabled(true);
                } else {
                    mStartService.setEnabled(false);
                    service_msg="";
                    webView.loadData(starting_msg, "text/html", "UTF-8");
                    startForegroundService();
                    mStartService.setText("Stop Service");
                }
            }
        });
        memoryInfo = binding.memoryInfo;
        memoryInfoSub = binding.memoryInfoSub;
        webView = binding.sampleText;
        ipView = binding.ipView;
        ipView.setOnLongClickListener(new View.OnLongClickListener() {
            @Override
            public boolean onLongClick(View v) {
                ClipData clip = ClipData.newPlainText("ipInfo", ipView.getText());
                mClipboard.setPrimaryClip(clip);
                Toast.makeText(MainActivity.this, "the IP information has been copied.", Toast.LENGTH_SHORT).show();
                return true;
            }
        });

        SharedPreferences sharedPreferences = getSharedPreferences("LogLevel",Context.MODE_PRIVATE);
        mLogLevelIndex = sharedPreferences.getInt("level",2);
        mUpdateMemThread.start();
        displayMemoryInfo();
        updateIpView();
        webView.loadData(stop_msg, "text/html", "UTF-8");
        LogUtils.logDebug(TAG,"onCreate end",LogUtils.LOG_DEBUG);
    }


    @Override
    protected void onDestroy() {
        super.onDestroy();
        mExitThread = true;
    }

    @Override
    protected void onResume() {
        super.onResume();
        invalidateOptionsMenu();
        updateWebView();
        updateIpView();
    }

    public static void updateWebView(String str) {
        LogUtils.logDebug(TAG,"updateWebView start : " + str,LogUtils.LOG_DEBUG);
        service_msg = str;
    }

    @Override
    public boolean onPrepareOptionsMenu(Menu menu) {
        menu.clear();
        File logDir = new File(LogUtils.LOG_DIRECTORY);
        File[] files = logDir.listFiles();
        if (files != null && files.length > 0) {
            SubMenu logFiles = menu.addSubMenu("Log Files");
            for (int i = 0; i < files.length; i++) {
                logFiles.add(files[i].getName()).setOnMenuItemClickListener(new MenuItem.OnMenuItemClickListener() {
                    @Override
                    public boolean onMenuItemClick(@NonNull MenuItem menuItem) {
                        Intent intent = new Intent(getApplicationContext(), LogContentActivity.class);
                        intent.putExtra("log_name",menuItem.toString());
                        startActivity(intent);
                        return false;
                    }
                });
            }
        }
        SubMenu logLevel = menu.addSubMenu("Log Level");
        for(int i = 0; i < logLevelItems.length; i++) {
            MenuItem menuItem = null;
            if (mLogLevelIndex == (i + 1)) {
                menuItem = logLevel.add(logLevelItems[i] + " ✓");
            } else {
                menuItem = logLevel.add(logLevelItems[i]);
            }
            menuItem.setOnMenuItemClickListener(new MenuItem.OnMenuItemClickListener() {
                @Override
                public boolean onMenuItemClick(@NonNull MenuItem menuItem) {
                    int index = getLogLevel(menuItem.toString());
                    LogUtils.logDebug(TAG,"menuItem.getItemId() = " + index,LogUtils.LOG_DEBUG);
                    if (index != -1) {
                        SharedPreferences sharedPreferences = getSharedPreferences("LogLevel", Context.MODE_PRIVATE);
                        SharedPreferences.Editor editor = sharedPreferences.edit();
                        editor.putInt("level", index);
                        editor.apply();
                        mLogLevelIndex = index;
                        if (ForegroundService.ServiceIsRunning) {
                            showDialog();
                        }
                    }
                    return false;
                }
            });
        }
        return super.onPrepareOptionsMenu(menu);
    }

    public int getLogLevel(String str) {
        for(int i = 0; i < logLevelItems.length; i++) {
            if (logLevelItems[i].equals(str)) {
                return i+1;
            }
        }
        return -1;
    }

    public void showDialog() {
        AlertDialog.Builder tipDialog = new AlertDialog.Builder(this);
        tipDialog.setMessage(getString(R.string.tips));
        tipDialog.setPositiveButton("OK", new DialogInterface.OnClickListener() {
            @Override
            public void onClick(DialogInterface dialogInterface, int i) {
                new Thread(new Runnable() {
                    @Override
                    public void run() {
                        if (ForegroundService.ServiceIsRunning) {
                            runOnUiThread(new Runnable() {
                                @Override
                                public void run() {
                                    mStartService.setEnabled(false);
                                    stopForegroundService();
                                    mStartService.setText("Start Service");
                                    service_msg="";
                                    webView.loadData(stop_msg, "text/html", "UTF-8");
                                }
                            });
                            try {
                                Thread.sleep(2000);
                            } catch (InterruptedException e) {
                                e.printStackTrace();
                            }
                            runOnUiThread(new Runnable() {
                                @Override
                                public void run() {
                                    startForegroundService();
                                    mStartService.setText("Stop Service");
                                    service_msg="";
                                    webView.loadData(starting_msg, "text/html", "UTF-8");
                                }
                            });
                        }
                    }
                }).start();
            }
        });
        tipDialog.create().show();
    }
}