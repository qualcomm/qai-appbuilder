package com.example.genieapiservice;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.view.Menu;
import android.view.MenuItem;
import android.view.SubMenu;
import android.view.View;
import android.widget.Button;
import android.widget.ScrollView;
import android.widget.TextView;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.appcompat.widget.Toolbar;
import androidx.core.content.FileProvider;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.FileReader;
import java.io.IOException;
import java.io.InputStreamReader;

public class LogContentActivity extends AppCompatActivity {

    private final static String TAG = "LogContentActivity";
    private final int mLogLinesMax = 600;
    private ScrollView mScrollView;
    private TextView mLogContent;
    private ClipboardManager mClipboard;
    private File mCurrentLogFile = null;
    private String mLogName;
    private String mLogPath;
    private TextView mTile;
    private TextView mBeforeButton;
    private TextView mAfterButton;
    private String mLogDirectory;
    private Button mRefreshButton;
    private Button mPreviousButton;
    private Button mNextButton;
    private TextView mPageIndex;
    private int mTotalPageNumber;
    private int mCurrentPageIndex;
    private int mCurrentLineNumber;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_log_content);
        setSupportActionBar(findViewById(R.id.toolbar));
        getSupportActionBar().setDisplayShowTitleEnabled(false);
        mScrollView = findViewById(R.id.scrollview);
        mLogContent = findViewById(R.id.log_content);
        mBeforeButton = findViewById(R.id.before);
        mPreviousButton = findViewById(R.id.previous);
        mPreviousButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                LogUtils.logDebug(TAG,"mPreviousButton clicked",LogUtils.LOG_DEBUG);
                if (mCurrentPageIndex >= 1) {
                    if (mCurrentPageIndex != 1) {
                        mCurrentPageIndex--;
                    } else {
                        return;
                    }
                    int begin = (mCurrentPageIndex-1)*mLogLinesMax + 1;
                    showLogContentByLines(new File(mLogPath),begin);
                }
            }
        });
        mNextButton = findViewById(R.id.next);
        mNextButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                LogUtils.logDebug(TAG,"mNextButton clicked",LogUtils.LOG_DEBUG);
                if (mCurrentPageIndex <= mTotalPageNumber) {
                    if (mCurrentPageIndex != mTotalPageNumber) {
                        mCurrentPageIndex++;
                    } else {
                        return;
                    }
                    int begin = (mCurrentPageIndex-1)*mLogLinesMax + 1;
                    showLogContentByLines(new File(mLogPath),begin);
                }
            }
        });
        mPageIndex = findViewById(R.id.log_index);
        mBeforeButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                File file = getLogFileIndex(mCurrentLogFile,false);
                if (file != null) {
                    //showLogContent(file);
                    mLogPath = file.getAbsolutePath();
                    mTotalPageNumber = getPageNumber(mLogPath);
                    mCurrentPageIndex = mTotalPageNumber;
                    int begin = (mCurrentPageIndex-1)*mLogLinesMax + 1;
                    showLogContentByLines(file,begin);
                    mLogName = file.getName();
                    mTile.setText(mLogName);
                    LogUtils.logDebug(TAG,"mBeforeButton clicked ; mLogName = "
                            + mLogName + "  mLogPath = " + mLogPath + " mTotalPageNumber = " + mTotalPageNumber + " begin = " + begin,LogUtils.LOG_DEBUG);
                }
            }
        });
        mAfterButton = findViewById(R.id.after);
        mAfterButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                File file = getLogFileIndex(mCurrentLogFile,true);
                if (file != null) {
                    //showLogContent(file);
                    mLogPath = file.getAbsolutePath();
                    mTotalPageNumber = getPageNumber(mLogPath);
                    mCurrentPageIndex = mTotalPageNumber;
                    int begin = (mCurrentPageIndex-1)*mLogLinesMax + 1;
                    showLogContentByLines(file,begin);
                    mLogName = file.getName();
                    mTile.setText(mLogName);
                    LogUtils.logDebug(TAG,"mAfterButton clicked ; mCurrentLogFile = "
                            + mCurrentLogFile + "  mLogPath = " + mLogPath + " mTotalPageNumber = " + mTotalPageNumber + " begin = " + begin,LogUtils.LOG_DEBUG);
                }
            }
        });
        mRefreshButton = findViewById(R.id.refresh);
        mRefreshButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                File file = mCurrentLogFile;
                if (file != null) {
                    mLogPath = file.getAbsolutePath();
                    mTotalPageNumber = getPageNumber(mLogPath);
                    mCurrentPageIndex = mTotalPageNumber;
                    int begin = (mCurrentPageIndex-1)*mLogLinesMax + 1;
                    showLogContentByLines(file,begin);
                    mLogName = file.getName();
                    mTile.setText(mLogName);
                    mScrollView.fullScroll(ScrollView.FOCUS_DOWN);
                }
            }
        });
        mClipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        mLogName = getIntent().getExtras().getString("log_name");
        LogUtils.logDebug(TAG,"mLogName = " + mLogName,LogUtils.LOG_DEBUG);
        mLogPath = LogUtils.LOG_DIRECTORY + File.separator + mLogName;
        mTile = findViewById(R.id.title);
        mTile.setText(mLogName);
        mTotalPageNumber = getPageNumber(mLogPath);
        mCurrentPageIndex = mTotalPageNumber;
    }


    private int getPageNumber(String file) {
        int lineCount = 0;
        try {
            BufferedReader reader = new BufferedReader(new FileReader(file));
            while (reader.readLine() != null) {
                lineCount++;
            }
            reader.close();
        } catch (IOException e) {
            e.printStackTrace();
        }
        mCurrentLineNumber = lineCount;
        if (lineCount%mLogLinesMax == 0) {
            return lineCount/mLogLinesMax;
        } else {
            return lineCount/mLogLinesMax + 1;
        }
    }

    /*private void showLogContent(File log) {
        Log.e("","logFile = " + log.getAbsolutePath() + " log size = " + log.length());
        StringBuilder stringBuilder = new StringBuilder();
        FileInputStream input = null;
        try {
            input = new FileInputStream(log);
            byte[] tem = new byte[1024];
            int len = 0;
            while ((len = input.read(tem)) > 0) {
                stringBuilder.append(new String(tem,0,len));
            }
            input.close();
            mLogContent.setText(stringBuilder.toString());
            mScrollView.fullScroll(ScrollView.FOCUS_DOWN);
            mLogContent.invalidate();
            mCurrentLogFile = log;
        } catch (Exception exception) {
            exception.printStackTrace();
        }
    }*/

    private void showLogContentByLines(File log,int beginLine) {
        LogUtils.logDebug(TAG,"logFile = " + log.getAbsolutePath() + " beginLine = " + beginLine,LogUtils.LOG_DEBUG);
        int lineNumber = 0;
        StringBuilder stringBuilder = new StringBuilder();
        try {
            FileInputStream fis = new FileInputStream(log.getAbsolutePath());
            InputStreamReader isr = new InputStreamReader(fis);
            BufferedReader br = new BufferedReader(isr);
            String line;
            while ((line = br.readLine()) != null) {
                if (line.equals("\n")) {
                    continue;
                }
                lineNumber++;
                if (lineNumber < beginLine) {
                    continue;
                }
                if (lineNumber < beginLine+mLogLinesMax) {
                    stringBuilder.append(line);
                    stringBuilder.append("\n");
                } else {
                    break;
                }
            }
            br.close();
            isr.close();
            fis.close();
            mLogContent.setText(stringBuilder.toString());
            mTotalPageNumber = getPageNumber(mLogPath);
            String pageInfo = mCurrentPageIndex + "/" + mTotalPageNumber;
            mPageIndex.setText(pageInfo);
            //mScrollView.fullScroll(ScrollView.FOCUS_DOWN);
            mLogContent.invalidate();
            mCurrentLogFile = log;
        } catch (FileNotFoundException e) {
            e.printStackTrace();
        } catch (IOException e) {
            e.printStackTrace();
        }
    }

    private File getLogFileIndex(File log,boolean next) {
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
        for (int i = files.length - 1; i >= 0; i--) {
            if (files[i].getName().equals(log.getName())) {
                if (next) {
                    index = Integer.valueOf(files[i].getName().substring(4)).intValue();
                    if ((index + 1) > files.length) {
                        break;
                    }
                    String newName = files[i].getName().substring(0, 4) + String.valueOf(index + 1);
                    File newFile = new File(mLogDirectory + File.separator + newName);
                    return newFile;
                } else {
                    index = Integer.valueOf(files[i].getName().substring(4)).intValue();
                    if ((index - 1) < 1) {
                        break;
                    }
                    String newName = files[i].getName().substring(0, 4) + String.valueOf(index - 1);
                    File newFile = new File(mLogDirectory + File.separator + newName);
                    return newFile;
                }
            }
        }
        return null;
    }

    @Override
    protected void onResume() {
        super.onResume();
        int begin = (mCurrentPageIndex-1)*mLogLinesMax + 1;
        showLogContentByLines(new File(mLogPath),begin);
    }

    public void shareLogContent() {
        int lineNumber = 0;
        StringBuilder stringBuilder = new StringBuilder();
        try {
            FileInputStream fis = new FileInputStream(mCurrentLogFile.getAbsolutePath());
            InputStreamReader isr = new InputStreamReader(fis);
            BufferedReader br = new BufferedReader(isr);
            String line;
            while ((line = br.readLine()) != null) {
                lineNumber++;
                if (lineNumber <= (mCurrentLineNumber -mLogLinesMax)) {
                    continue;
                }
                stringBuilder.append(line);
                stringBuilder.append("\n");
            }
            br.close();
            isr.close();
            fis.close();
        } catch (FileNotFoundException e) {
            e.printStackTrace();
        } catch (IOException e) {
            e.printStackTrace();
        }
        Intent intent = new Intent(Intent.ACTION_SEND);
        intent.setType("text/plain");
        //intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        //intent.putExtra(Intent.EXTRA_STREAM, Uri.fromFile(mCurrentLogFile));
        intent.putExtra(Intent.EXTRA_TEXT, stringBuilder.toString());
        //intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        startActivity(Intent.createChooser(intent,"Share Log Content"));
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        /*menu.add("Refresh Log").setOnMenuItemClickListener(new MenuItem.OnMenuItemClickListener() {
            @Override
            public boolean onMenuItemClick(@NonNull MenuItem menuItem) {
                showLogContent(mCurrentLogFile);
                return false;
            }
        });*/
        menu.add("Copy Log").setOnMenuItemClickListener(new MenuItem.OnMenuItemClickListener() {
            @Override
            public boolean onMenuItemClick(@NonNull MenuItem menuItem) {
                ClipData clip = ClipData.newPlainText("LogInfo", mLogContent.getText());
                mClipboard.setPrimaryClip(clip);
                return false;
            }
        });
        menu.add("Share Log").setOnMenuItemClickListener(new MenuItem.OnMenuItemClickListener() {
            @Override
            public boolean onMenuItemClick(@NonNull MenuItem menuItem) {
                shareLogContent();
                return false;
            }
        });
        return super.onCreateOptionsMenu(menu);
    }
}