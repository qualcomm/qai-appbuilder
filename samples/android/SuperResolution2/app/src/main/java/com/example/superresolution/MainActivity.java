//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.example.superresolution;

import androidx.appcompat.app.AppCompatActivity;
import androidx.annotation.Nullable;

import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.os.Bundle;
import android.provider.DocumentsContract;
import android.provider.MediaStore;
import android.provider.Settings;
import android.system.Os;
import android.util.Log;
import android.view.View;
//import android.widget.TextView;
import android.widget.*;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.FloatBuffer;
import java.util.ArrayList;

import org.opencv.android.OpenCVLoader;
import org.opencv.android.Utils;
import org.opencv.core.Core;
import org.opencv.core.CvType;
import org.opencv.core.Mat;
import org.opencv.core.Size;
import org.opencv.imgproc.Imgproc;


import com.example.superresolution.databinding.ActivityMainBinding;

public class MainActivity extends AppCompatActivity {

    // Used to load the 'superresolution' library on application startup.
    static {
        System.loadLibrary("superresolution");
    }

    private ActivityMainBinding binding;
    private static final int PICK_IMAGE_REQUEST = 1;
    private ImageView inputImagePreview, outputImagePreview;
    private Spinner spinner_models;
 	private ArrayList<String> CV_Models = new ArrayList<>();
    private String nativeLibPath = "";
	private String cv_path = "/sdcard/AIModels/SuperResolution/";
	private ArrayAdapter<String> adapter;
	private String TAG = "superresolution";
	private boolean DEBUG = false;
	private String selectedItem = "";
	private String input_img = "";
    private String output_img = "";
    private File rootDir;
    private static final int IMAGE_WIDTH = 128;
    private static final int IMAGE_HEIGHT = 128;
    private static final int SCALE = 4;
    public void requestPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager()) {
                Intent intent = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION);
                intent.setData(Uri.parse("package:" + getPackageName()));
                startActivity(intent);
            }
        }
    }

    public void listBinFiles(File currentDir, File rootDir) {
        if(DEBUG) Log.d(TAG,"listBinFiles begin");
        File[] files = currentDir.listFiles();
        if (files == null) return;

        for (File file : files) {
            if (file.isDirectory()) {
                listBinFiles(file, rootDir);
            } else if (file.isFile() && file.getName().endsWith(".bin")) {
                String relativePath = rootDir.toURI().relativize(file.toURI()).getPath();
				if(DEBUG) Log.d(TAG,"find bin files：" + relativePath);
				CV_Models.add(relativePath.replaceFirst("\\.bin$", ""));
				Log.d(TAG,"listBinFiles, CV_Models:" + CV_Models);
            }
        }
    }
	
    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if(DEBUG) Log.d(TAG,"onCreate ");

        if (!OpenCVLoader.initDebug()) {
            Log.e(TAG, "OpenCV initialization failed!");
        } else {
            Log.d(TAG, "OpenCV initialization successful!");
        }
		
        binding = ActivityMainBinding.inflate(getLayoutInflater());
        setContentView(binding.getRoot());

        requestPermission();

        try {
            nativeLibPath = getApplicationContext().getApplicationInfo().nativeLibraryDir;
            Os.setenv("ADSP_LIBRARY_PATH", nativeLibPath, true);
            Os.setenv("LD_LIBRARY_PATH", nativeLibPath, true);
        } catch (Exception e) {
        }
        if(DEBUG) Log.d(TAG,"nativeLibPath: " + nativeLibPath);

        inputImagePreview = findViewById(R.id.inputImagePreview);
        outputImagePreview = findViewById(R.id.outputImagePreview);
        Button selectImageButton = findViewById(R.id.selectImageButton);
        Button covertImageButton = findViewById(R.id.covertImageButton);
		
        selectImageButton.setOnClickListener(v -> {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            intent.setType("image/*");
            startActivityForResult(intent, PICK_IMAGE_REQUEST);
        });

        covertImageButton.setOnClickListener(v -> {
            Log.d(TAG,"covertImageButton,input_img=" + input_img);
            File input_imgFile = new File(input_img);
            if (!input_imgFile.exists()) {
                Toast.makeText(getApplicationContext(),"Please select input image first!", Toast.LENGTH_SHORT).show();
                return;
            }

            String model_name = cv_path + selectedItem + ".bin";
            Log.d(TAG,"covertImageButton,model_name=" + model_name);

            output_img = cv_path + selectedItem + "_output.jpg";
            if(DEBUG) Log.d(TAG,"covertImageButton,output_img=" + output_img);

            Bitmap bitmap = BitmapFactory.decodeFile(input_img);
            Bitmap resizedBitmap = Bitmap.createScaledBitmap(bitmap, IMAGE_WIDTH, IMAGE_HEIGHT, true);

            ByteBuffer inputBuffer = preprocess(resizedBitmap);

            int srWidth = SCALE * IMAGE_WIDTH;
            int srHeight = SCALE * IMAGE_HEIGHT;
            int outputBufferSize = srWidth * srHeight * 3 * 4; // float32
            ByteBuffer outputBuffer = ByteBuffer.allocateDirect(outputBufferSize);
            outputBuffer.order(ByteOrder.nativeOrder());

            SuperResolution(nativeLibPath, model_name, inputBuffer, outputBuffer);

            Bitmap outputBitmap = postprocess(outputBuffer, srWidth, srHeight);
            outputImagePreview.setImageBitmap(outputBitmap);

            try (FileOutputStream out = new FileOutputStream(output_img)) {
                outputBitmap.compress(Bitmap.CompressFormat.JPEG, 100, out);
            } catch (IOException e) {
                e.printStackTrace();
            }
        });

		if(DEBUG) Log.d("superresolution","CV_Models: " + CV_Models);
        showBinFilesList();
	
        TextView textView_models = binding.models;
        textView_models.setText("Select Model:");
    }

	public String getRealPathFromUri(Context context, Uri uri) {
		String path = null;

		// DocumentProvider
		if (DocumentsContract.isDocumentUri(context, uri)) {
			String docId = DocumentsContract.getDocumentId(uri);
			String[] split = docId.split(":");
			String type = split[0];

			if ("primary".equalsIgnoreCase(type)) {
				path = Environment.getExternalStorageDirectory() + "/" + split[1];
			} else if ("image".equals(type)) {
				Uri contentUri = MediaStore.Images.Media.EXTERNAL_CONTENT_URI;
				String selection = "_id=?";
				String[] selectionArgs = new String[]{ split[1] };
				path = getDataColumn(context, contentUri, selection, selectionArgs);
			}
		}
		// MediaStore (and general)
		else if ("content".equalsIgnoreCase(uri.getScheme())) {
			path = getDataColumn(context, uri, null, null);
		}
		// File
		else if ("file".equalsIgnoreCase(uri.getScheme())) {
			path = uri.getPath();
		}

		return path;
	}

	private String getDataColumn(Context context, Uri uri, String selection, String[] selectionArgs) {
		Cursor cursor = null;
		final String column = "_data";
		final String[] projection = { column };

		try {
			cursor = context.getContentResolver().query(uri, projection, selection, selectionArgs, null);
			if (cursor != null && cursor.moveToFirst()) {
				final int index = cursor.getColumnIndexOrThrow(column);
				return cursor.getString(index);
			}
		} finally {
			if (cursor != null)
				cursor.close();
		}
		return null;
	}

    private void showBinFilesList(){
		if(DEBUG) Log.d(TAG, "showBinFilesList, CV_Models = "+CV_Models);
		rootDir = new File(cv_path);
        if (rootDir.exists() && rootDir.isDirectory()) {
            CV_Models.clear();
            listBinFiles(rootDir, rootDir);
        } else {
            if(DEBUG) Log.d(TAG, cv_path + "is not existed!!!");
			Toast.makeText(getApplicationContext(),cv_path + "is not existed!", Toast.LENGTH_SHORT).show();
        }
		
		adapter = new ArrayAdapter<>(this,
                android.R.layout.simple_spinner_item, 0, CV_Models);
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);		
		spinner_models = findViewById(R.id.spinnerModelsList);
        spinner_models.setAdapter(adapter);

        spinner_models.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                selectedItem = parent.getItemAtPosition(position).toString();
                if(DEBUG) Log.d("SpinnerSelection", "selected model：" + selectedItem);
                Toast.makeText(getApplicationContext(),selectedItem, Toast.LENGTH_SHORT).show();
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {
                Log.d("SpinnerSelection", "please select a model");
            }
        });
	}
	
    @Override
    public void onResume(){
        super.onResume();
        if(DEBUG) Log.d(TAG,"onResume,CV_Models=" + CV_Models);
		if(CV_Models.isEmpty()){
			showBinFilesList();
		}
    }
	
    @Override
    protected void onActivityResult(int requestCode, int resultCode,
                                    @Nullable Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if(DEBUG) Log.d(TAG,"requestCode=" + requestCode);
        if (requestCode == PICK_IMAGE_REQUEST && resultCode == RESULT_OK && data != null) {
            Uri imageUri = data.getData();
            if(DEBUG) Log.d(TAG,"imageUri=" + imageUri);
			input_img = getRealPathFromUri(getApplicationContext(), imageUri);
			Log.d(TAG,"onActivityResult, input_img=" + input_img);

            File input_imgFile = new File(input_img);
            if (input_imgFile.exists()) {
                Bitmap bitmap = BitmapFactory.decodeFile(input_imgFile.getAbsolutePath());
                inputImagePreview.setImageBitmap(bitmap);
            }
        }
    }
    /**
     * A native method that is implemented by the 'superresolution' native library,
     * which is packaged with this application.
     */
    public native int SuperResolution(String libsDir, String model_path, ByteBuffer inputBuffer, ByteBuffer outputBuffer);

    private ByteBuffer preprocess(Bitmap bitmap) {
        Mat mat = new Mat();
        Utils.bitmapToMat(bitmap, mat);
        Imgproc.cvtColor(mat, mat, Imgproc.COLOR_BGR2RGB);
        mat.convertTo(mat, CvType.CV_32F, 1.0 / 255.0);

        int bufferSize = IMAGE_WIDTH * IMAGE_HEIGHT * 3 * 4; // float32
        ByteBuffer byteBuffer = ByteBuffer.allocateDirect(bufferSize);
        byteBuffer.order(ByteOrder.nativeOrder());
        FloatBuffer floatBuffer = byteBuffer.asFloatBuffer();

        float[] data = new float[IMAGE_WIDTH * IMAGE_HEIGHT * 3];
        mat.get(0, 0, data);
        floatBuffer.put(data);

        return byteBuffer;
    }

    private Bitmap postprocess(ByteBuffer buffer, int width, int height) {
        buffer.rewind();
        FloatBuffer floatBuffer = buffer.asFloatBuffer();
        float[] data = new float[width * height * 3];
        floatBuffer.get(data);

        for (int i = 0; i < data.length; i++) {
            data[i] = Math.max(0.0f, Math.min(255.0f, data[i] * 255.0f));
        }

        Mat mat = new Mat(height, width, CvType.CV_32FC3);
        mat.put(0, 0, data);
        mat.convertTo(mat, CvType.CV_8UC3);
        Imgproc.cvtColor(mat, mat, Imgproc.COLOR_RGB2BGR);

        Bitmap bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888);
        Utils.matToBitmap(mat, bitmap);

        return bitmap;
    }
}
