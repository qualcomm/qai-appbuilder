package com.example.genieapiservice;

import androidx.appcompat.app.AppCompatActivity;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.Settings;
import android.webkit.WebView;
import android.widget.TextView;
import android.system.Os;
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
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.Enumeration;
import com.example.genieapiservice.databinding.ActivityMainBinding;

class MyNativeLib {
    public native void runService(String[] args);
}

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

    static {
        System.loadLibrary("genieapiservice");
        System.loadLibrary("JNIGenieAPIService");
    }

    private ActivityMainBinding binding;
    private WebView webView;
    private TextView ipView;
    private String service_msg = "";

    private Handler handler = new Handler(Looper.getMainLooper()) {
        @Override
        public void handleMessage(Message msg) {
            if (msg.what == 1) {
                // webView.setText(service_msg);
                webView.loadData(service_msg, "text/html", "UTF-8");

                String ipAddress = IPAddressUtils.getIPAddress(MainActivity.this);
                if (ipAddress != null) {
                    ipView.setText("IP Address & Port: " + ipAddress + ":8910");
                } else {
                    ipView.setText("Can't get IP Address.");
                }
            }
        }
    };

    public void requestPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager()) {
                Intent intent = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION);
                intent.setData(Uri.parse("package:" + getPackageName()));
                startActivity(intent);
            }
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        binding = ActivityMainBinding.inflate(getLayoutInflater());
        setContentView(binding.getRoot());

        requestPermission();

        try {
            String nativeLibPath = getApplicationContext().getApplicationInfo().nativeLibraryDir;
            Os.setenv("ADSP_LIBRARY_PATH", nativeLibPath, true);
            Os.setenv("LD_LIBRARY_PATH", nativeLibPath, true);
        } catch (Exception e) {
        }

        // Start Service
        new Thread(() -> {
            String[] commandArgs = {"main", "-c", "/sdcard/GenieModels/qwen2.0_7b-ssd/config.json", "-l"};
            System.out.println("commandArgs: " + commandArgs[1]);
            MyNativeLib nativeLib = new MyNativeLib();
            nativeLib.runService(commandArgs);
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
                    if (responseCode == HttpURLConnection.HTTP_OK) {
                        Scanner scanner = new Scanner(connection.getInputStream());
                        StringBuilder response = new StringBuilder();
                        while (scanner.hasNextLine()) {
                            response.append(scanner.nextLine());
                        }
                        scanner.close();

                        System.out.println("Response: " + response.toString());
                        service_msg = response.toString();

                        Message message = new Message();
                        message.what = 1;
                        handler.sendMessage(message);
                        break;
                    }
                } catch (IOException | InterruptedException e) {
                    e.printStackTrace();
                }
            }
        }).start();

        webView = binding.sampleText;
        webView.loadData(stringFromJNI(), "text/html", "UTF-8");
        ipView = binding.ipView;
    }

    public native String stringFromJNI();
    public native void runService(String[] args);
}