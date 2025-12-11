package com.example.genieapiservice;

import android.animation.ValueAnimator;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.os.Handler;
import android.os.IBinder;
import android.graphics.PixelFormat;
import android.os.Message;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.view.animation.Animation;
import android.view.animation.AnimationUtils;
import android.widget.ImageView;

import androidx.annotation.NonNull;


public class FloatingService extends Service {

    private final String TAG = "FloatingService";
    private WindowManager windowManager;
    private ImageView floatingView;
    private final long MaxClickTime = 200;
    private long startTouchTime;
    private int screenWidth;
    private WindowManager.LayoutParams layoutParams;
    private boolean isHidden = false;
    private boolean isMoving;
    private boolean isAligning;
    private Handler mHandler = new Handler() {
        @Override
        public void handleMessage(@NonNull Message msg) {
            super.handleMessage(msg);
            switch (msg.what) {
                case 0:
                    isHidden = true;
                    floatingView.animate().translationX(-floatingView.getWidth() * 4f / 5f).setDuration(300).start();
                    break;
                case 1:
                    isHidden = true;
                    floatingView.animate().translationX(floatingView.getWidth() * 4f / 5f).setDuration(300).start();
                    break;
                case 3:
                    isHidden = false;
                    floatingView.animate().translationX(0).setDuration(300).start();
                    break;
                default:
                    break;
            }
        }
    };

    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        screenWidth = this.getResources().getDisplayMetrics().widthPixels;
        autoAlignAndHide();
    }

    @Override
    public void onCreate() {
        super.onCreate();
        SharedPreferences sharedPref = this.getSharedPreferences("position",Context.MODE_PRIVATE);
        screenWidth = this.getResources().getDisplayMetrics().widthPixels;
        windowManager = (WindowManager) getSystemService(WINDOW_SERVICE);
        floatingView = (ImageView) LayoutInflater.from(this).inflate(R.layout.layout_floating, null);
        layoutParams = new WindowManager.LayoutParams(
                120,
                120,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
                PixelFormat.TRANSLUCENT
        );
        layoutParams.gravity = Gravity.TOP | Gravity.START;
        layoutParams.x = sharedPref.getInt("x",0);
        layoutParams.y = sharedPref.getInt("y",100);
        windowManager.addView(floatingView, layoutParams);
        if (layoutParams.x == 0) {
            mHandler.sendEmptyMessageDelayed(0,2000);
        } else {
            mHandler.sendEmptyMessageDelayed(1,2000);
        }
        floatingView.setOnTouchListener(new View.OnTouchListener() {
            private float initialTouchX,initialTouchY;
            @Override
            public boolean onTouch(View v, MotionEvent event) {
                switch (event.getAction()){
                    case MotionEvent.ACTION_DOWN:
                        isMoving = false;
                        startTouchTime = System.currentTimeMillis();
                        initialTouchX = event.getRawX();
                        initialTouchY = event.getRawY();
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        isMoving = true;
                        if (mHandler.hasMessages(0)) {
                            mHandler.removeMessages(0);
                        }
                        if (mHandler.hasMessages(1)) {
                            mHandler.removeMessages(1);
                        }
                        if (isHidden) {
                            restoreIcon();
                        }
                        int detailX = (int) (event.getRawX()-initialTouchX);
                        int detailY = (int) (event.getRawY()-initialTouchY);
                        layoutParams.x +=detailX;
                        layoutParams.y +=detailY;
                        windowManager.updateViewLayout(floatingView,layoutParams);
                        initialTouchX = event.getRawX();
                        initialTouchY = event.getRawY();
                        return true;
                    case MotionEvent.ACTION_UP:
                        isMoving = false;
                        long touchTime = System.currentTimeMillis()-startTouchTime;
                        if(touchTime < MaxClickTime){
                            v.performClick();
                        } else {
                            autoAlignAndHide();
                        }
                }
                return false;
            }
        });
        floatingView.setOnClickListener(v -> {
            if (isHidden) {
                restore();
            } else {
                if (!isMoving && !isAligning) {
                    Intent intent = new Intent(FloatingService.this, MainActivity.class);
                    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(intent);
                }
            }
        });
    }

    private void autoAlignAndHide() {
        int targetX;
        if (layoutParams.x < screenWidth / 2) {
            // align left → move left
            targetX = 0;
            animateWindow(layoutParams.x, targetX);
        } else {
            // align right → move right
            targetX = screenWidth - layoutParams.width;
            animateWindow(layoutParams.x, targetX);
        }
    }

    private void restore() {
        int targetX;
        if (layoutParams.x < screenWidth / 2) {
            targetX = 0;
        } else {
            targetX = screenWidth - layoutParams.width;
        }
        animateWindow(layoutParams.x, targetX);
        mHandler.sendEmptyMessage(3);
    }

    private void restoreIcon() {
        //floatingView.animate().translationX(0).setDuration(1).start();
        restore();
    }

    private void animateWindow(int fromX, int toX) {
        ValueAnimator animator = ValueAnimator.ofInt(fromX, toX);
        animator.setDuration(300);
        animator.addUpdateListener(animation -> {
            layoutParams.x = (int) animation.getAnimatedValue();
            if (floatingView.isAttachedToWindow()) {
                windowManager.updateViewLayout(floatingView, layoutParams);
            }
            if (layoutParams.x == toX) {
                if (layoutParams.x == 0) {
                    mHandler.sendEmptyMessageDelayed(0, 2000);
                } else {
                    mHandler.sendEmptyMessageDelayed(1, 2000);
                }
                isAligning = false;
            }
        });
        isAligning = true;
        animator.start();
    }


    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        try {
            SharedPreferences sharedPref = this.getSharedPreferences("position",Context.MODE_PRIVATE);
            SharedPreferences.Editor editor = sharedPref.edit();
            editor.putInt("x", layoutParams.x);
            editor.putInt("y", layoutParams.y);
            editor.apply();
            windowManager.removeView(floatingView);
            stopSelf();
        } catch (Exception exception) {
            exception.printStackTrace();
        }
        LogUtils.logDebug(TAG,"call onDestroy",LogUtils.LOG_DEBUG);
    }
}
