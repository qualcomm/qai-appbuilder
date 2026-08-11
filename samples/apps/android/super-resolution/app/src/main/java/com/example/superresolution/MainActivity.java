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

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.FloatBuffer;
import java.util.ArrayList;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

import org.opencv.android.OpenCVLoader;
import org.opencv.android.Utils;
import org.opencv.core.Core;
import org.opencv.core.CvType;
import org.opencv.core.Mat;
import org.opencv.core.Size;
import org.opencv.imgproc.Imgproc;


import android.os.Handler;

import androidx.appcompat.widget.Toolbar;

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
    private LinearLayout inferenceProgressLayout;
    private TextView inferenceStatusText;
    // Handler used to periodically re-trigger marquee on the Spinner selected view
    private final Handler marqueeHandler = new Handler();
    private Runnable marqueeRunnable;
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

    // Pre/post-processing mode. Java (default) does the pre/post-processing with
    // OpenCV-Java; C++ does it inside the native library with OpenCV + xtensor.
    private boolean useCppProcessing = false;

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
            } else if (file.isFile() &&
                       (file.getName().endsWith(".bin") || file.getName().endsWith(".dlc"))) {
                String relativePath = rootDir.toURI().relativize(file.toURI()).getPath();
                if(DEBUG) Log.d(TAG,"find model files：" + relativePath);
                // Keep the extension so the native layer can detect the model format
                CV_Models.add(relativePath);
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

        // Set the Toolbar as the ActionBar so it appears correctly below the status bar
        Toolbar toolbar = findViewById(R.id.toolbar);
        if (toolbar != null) {
            setSupportActionBar(toolbar);
        }

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
        inferenceProgressLayout = findViewById(R.id.inferenceProgressLayout);
        inferenceStatusText = findViewById(R.id.inferenceStatusText);
        Button selectImageButton = findViewById(R.id.selectImageButton);
        Button covertImageButton = findViewById(R.id.covertImageButton);

        RadioGroup processingModeGroup = findViewById(R.id.processingModeGroup);
        if (processingModeGroup != null) {
            processingModeGroup.setOnCheckedChangeListener((group, checkedId) -> {
                useCppProcessing = (checkedId == R.id.modeCpp);
                if(DEBUG) Log.d(TAG, "useCppProcessing=" + useCppProcessing);
            });
        }

        selectImageButton.setOnClickListener(v -> {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            intent.setType("image/*");
            startActivityForResult(intent, PICK_IMAGE_REQUEST);
        });

        covertImageButton.setOnClickListener(v -> {
            Log.d(TAG,"covertImageButton,input_img=" + input_img);

            // Guard: selectedItem must be a valid .bin or .dlc path.
            // If the model list shows a placeholder (permission not granted,
            // download in progress, etc.), block the conversion to prevent crash.
            if (selectedItem == null ||
                (!selectedItem.endsWith(".bin") && !selectedItem.endsWith(".dlc"))) {
                Toast.makeText(getApplicationContext(),
                        "No valid model selected.\nPlease select a .bin or .dlc model file.",
                        Toast.LENGTH_LONG).show();
                return;
            }

            File input_imgFile = new File(input_img);
            if (!input_imgFile.exists()) {
                Toast.makeText(getApplicationContext(),"Please select input image first!", Toast.LENGTH_SHORT).show();
                return;
            }

            // selectedItem already contains the relative path with extension
            // e.g. "real_esrgan_x4plus/real_esrgan_x4plus.fp16.bin" or
            //      "real_esrgan_x4plus/real_esrgan_x4plus.dlc"
            final String model_name = cv_path + selectedItem;
            Log.d(TAG,"covertImageButton,model_name=" + model_name);

            // Strip the model file extension to build the output image path
            String modelBaseName = selectedItem.replaceFirst("\\.(bin|dlc)$", "");
            output_img = cv_path + modelBaseName + "_output.jpg";
            if(DEBUG) Log.d(TAG,"covertImageButton,output_img=" + output_img);

            // Disable the button to prevent double-clicks during inference
            covertImageButton.setEnabled(false);
            covertImageButton.setText("Converting...");

            // Show progress indicator
            if (inferenceProgressLayout != null) {
                inferenceProgressLayout.setVisibility(View.VISIBLE);
                if (inferenceStatusText != null) {
                    inferenceStatusText.setText("Running inference on: " + selectedItem + "\nPlease wait...");
                }
            }

            // Run inference on a background thread to avoid ANR
            // (SNPE/QNN inference can take several seconds)
            final boolean cppMode = useCppProcessing;
            new Thread(() -> {
                if (cppMode) {
                    runCppProcessing(model_name);
                } else {
                    runJavaProcessing(model_name);
                }
                // Re-enable the button and hide progress on the UI thread when done
                runOnUiThread(() -> {
                    covertImageButton.setEnabled(true);
                    covertImageButton.setText("Convert");
                    if (inferenceProgressLayout != null) {
                        inferenceProgressLayout.setVisibility(View.GONE);
                    }
                });
            }).start();
        });

		if(DEBUG) Log.d("superresolution","CV_Models: " + CV_Models);
        showBinFilesList();

        TextView textView_models = binding.models;
        textView_models.setText("Select Model:");
    }

    // Java-based pre/post-processing: OpenCV-Java prepares the input tensor and
    // decodes the output tensor; the native library only runs the inference.
    // NOTE: called from a background thread; all UI updates must use runOnUiThread().
    private void runJavaProcessing(String model_name) {
        Bitmap bitmap = BitmapFactory.decodeFile(input_img);
        if (bitmap == null) {
            runOnUiThread(() -> Toast.makeText(getApplicationContext(),
                    "Failed to decode input image: " + input_img, Toast.LENGTH_LONG).show());
            return;
        }
        Bitmap resizedBitmap = Bitmap.createScaledBitmap(bitmap, IMAGE_WIDTH, IMAGE_HEIGHT, true);

        ByteBuffer inputBuffer = preprocess(resizedBitmap);

        int srWidth = SCALE * IMAGE_WIDTH;
        int srHeight = SCALE * IMAGE_HEIGHT;
        int outputBufferSize = srWidth * srHeight * 3 * 4; // float32
        ByteBuffer outputBuffer = ByteBuffer.allocateDirect(outputBufferSize);
        outputBuffer.order(ByteOrder.nativeOrder());

        int ret = SuperResolution(nativeLibPath, model_name, inputBuffer, outputBuffer);
        if (ret != 0) {
            Log.e(TAG, "SuperResolution inference failed, ret=" + ret + ", model=" + model_name);
            final int finalRet = ret;
            runOnUiThread(() -> Toast.makeText(getApplicationContext(),
                    "Inference failed (ret=" + finalRet + ").\n" +
                    "Model: " + model_name + "\n" +
                    "Check that the model file is valid and the correct backend library is present.",
                    Toast.LENGTH_LONG).show());
            return;
        }

        Bitmap outputBitmap = postprocess(outputBuffer, srWidth, srHeight);
        final Bitmap finalBitmap = outputBitmap;
        runOnUiThread(() -> outputImagePreview.setImageBitmap(finalBitmap));

        try (FileOutputStream out = new FileOutputStream(output_img)) {
            outputBitmap.compress(Bitmap.CompressFormat.JPEG, 100, out);
        } catch (IOException e) {
            e.printStackTrace();
        }
    }

    // C++-based pre/post-processing: the native library reads the input image,
    // does all pre/post-processing with OpenCV + xtensor, and writes the output
    // image file. Java only passes file paths and reloads the result.
    // NOTE: called from a background thread; all UI updates must use runOnUiThread().
    private void runCppProcessing(String model_name) {
        // Delete any stale output file so we can reliably detect failure
        // (if the file still exists after the call, it means the old result
        //  was kept, not that the current inference succeeded).
        File output_imgFile = new File(output_img);
        if (output_imgFile.exists()) {
            output_imgFile.delete();
        }

        SuperResolutionCpp(nativeLibPath, model_name, input_img, output_img);

        if (output_imgFile.exists()) {
            Bitmap bitmap = BitmapFactory.decodeFile(output_imgFile.getAbsolutePath());
            if (bitmap != null) {
                final Bitmap finalBitmap = bitmap;
                runOnUiThread(() -> outputImagePreview.setImageBitmap(finalBitmap));
            } else {
                runOnUiThread(() -> Toast.makeText(getApplicationContext(),
                        "Output image is invalid (decode failed): " + output_img,
                        Toast.LENGTH_LONG).show());
            }
        } else {
            Log.e(TAG, "C++ inference failed: output image not created. model=" + model_name);
            runOnUiThread(() -> Toast.makeText(getApplicationContext(),
                    "Inference failed: output image not created.\n" +
                    "Model: " + model_name + "\n" +
                    "Possible causes:\n" +
                    "  1. ModelInitialize failed (check backend .so files)\n" +
                    "  2. Missing libQnnHtpV79Stub.so / libQnnHtpV81Stub.so in libs\n" +
                    "  3. Model file is corrupted or incompatible",
                    Toast.LENGTH_LONG).show());
        }
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

    // Default model URL to download when no model files are found
    private static final String DEFAULT_MODEL_URL =
            "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/" +
            "real_esrgan_general_x4v3/releases/v0.59.0/real_esrgan_general_x4v3-qnn_dlc-float.zip";
    private static final String DEFAULT_MODEL_ZIP_NAME = "real_esrgan_general_x4v3-qnn_dlc-float.zip";

    // Sample input image to download alongside the default model
    private static final String DEFAULT_INPUT_IMAGE_URL =
            "https://raw.githubusercontent.com/qualcomm/qai-appbuilder/main/samples/" +
            "ComputerVision/Super_Resolution/real_esrgan_general_x4v3/input.jpg";
    private static final String DEFAULT_INPUT_IMAGE_NAME = "input.jpg";

    private void showBinFilesList(){
		if(DEBUG) Log.d(TAG, "showBinFilesList, CV_Models = "+CV_Models);
		rootDir = new File(cv_path);

        // Ensure the model directory exists
        if (!rootDir.exists()) {
            rootDir.mkdirs();
        }

        CV_Models.clear();

        // Check if we have storage permission before scanning.
        // On Android R+, MANAGE_EXTERNAL_STORAGE may not be granted yet on first launch.
        boolean hasPermission = true;
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            hasPermission = android.os.Environment.isExternalStorageManager();
        }

        if (!hasPermission) {
            // Permission not yet granted — show a message and request it
            Log.w(TAG, "Storage permission not granted, cannot scan model directory");
            CV_Models.add("(Grant 'All files access' permission and restart app)");
            Toast.makeText(getApplicationContext(),
                    "Please grant 'All files access' permission\nto load model files from:\n" + cv_path,
                    Toast.LENGTH_LONG).show();
            requestPermission();
        } else {
            listBinFiles(rootDir, rootDir);

            if (CV_Models.isEmpty()) {
                // No model files found — auto-download the default model
                Log.w(TAG, "No .bin/.dlc model files found, starting auto-download...");
                startAutoDownload();
                CV_Models.add("(Downloading default model, please wait...)");
            }
        }

        // Custom adapter: forces marquee scrolling on the selected item view
        adapter = new ArrayAdapter<String>(this, R.layout.spinner_item_scrolling, CV_Models) {
            @Override
            public View getView(int position, View convertView, android.view.ViewGroup parent) {
                View v = super.getView(position, convertView, parent);
                if (v instanceof TextView) {
                    final TextView tv = (TextView) v;
                    tv.setSelected(true);
                    // Post to ensure the view is laid out before starting marquee
                    tv.post(() -> tv.setSelected(true));
                }
                return v;
            }
        };
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        spinner_models = findViewById(R.id.spinnerModelsList);
        spinner_models.setAdapter(adapter);

        spinner_models.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                selectedItem = parent.getItemAtPosition(position).toString();
                if(DEBUG) Log.d("SpinnerSelection", "selected model：" + selectedItem);

                // Show the full model path in a scrollable AlertDialog so long paths
                // are always fully visible regardless of screen width.
                new android.app.AlertDialog.Builder(MainActivity.this)
                        .setTitle("Selected Model")
                        .setMessage(selectedItem)
                        .setPositiveButton("OK", null)
                        .show();
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {
                Log.d("SpinnerSelection", "please select a model");
            }
        });
	}

    /**
     * Start downloading the default model and sample input image in the background.
     * Shows a progress indicator during download and unzip,
     * then refreshes the model list and input image preview when done.
     */
    private void startAutoDownload() {
        // Show progress indicator with download status
        runOnUiThread(() -> {
            if (inferenceProgressLayout != null) {
                inferenceProgressLayout.setVisibility(View.VISIBLE);
                if (inferenceStatusText != null) {
                    inferenceStatusText.setText(
                            "No model found. Downloading default model and sample image...");
                }
            }
        });

        new Thread(() -> {
            boolean modelSuccess = false;
            boolean imageSuccess = false;
            String errorMsg = "";
            File zipFile = new File(rootDir, DEFAULT_MODEL_ZIP_NAME);
            final File inputImageFile = new File(rootDir, DEFAULT_INPUT_IMAGE_NAME);

            try {
                // ── Step 1a: Download the model zip ──────────────────────────
                Log.d(TAG, "Downloading model: " + DEFAULT_MODEL_URL);
                runOnUiThread(() -> {
                    if (inferenceStatusText != null)
                        inferenceStatusText.setText(
                                "Downloading model...\n" + DEFAULT_MODEL_ZIP_NAME);
                });

                URL modelUrl = new URL(DEFAULT_MODEL_URL);
                HttpURLConnection modelConn = (HttpURLConnection) modelUrl.openConnection();
                modelConn.setConnectTimeout(15000);
                modelConn.setReadTimeout(60000);
                modelConn.connect();

                if (modelConn.getResponseCode() != HttpURLConnection.HTTP_OK) {
                    throw new IOException("Model HTTP error: " + modelConn.getResponseCode());
                }

                long totalBytes = modelConn.getContentLengthLong();
                long downloadedBytes = 0;

                try (InputStream in = new BufferedInputStream(modelConn.getInputStream());
                     FileOutputStream fos = new FileOutputStream(zipFile)) {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) != -1) {
                        fos.write(buf, 0, n);
                        downloadedBytes += n;
                        if (totalBytes > 0) {
                            final int pct = (int)(downloadedBytes * 100 / totalBytes);
                            runOnUiThread(() -> {
                                if (inferenceStatusText != null)
                                    inferenceStatusText.setText(
                                            "Downloading model... " + pct + "%\n" +
                                            DEFAULT_MODEL_ZIP_NAME);
                            });
                        }
                    }
                }
                modelConn.disconnect();
                Log.d(TAG, "Model download complete: " + zipFile.getAbsolutePath());

                // ── Step 1b: Download the sample input image ─────────────────
                Log.d(TAG, "Downloading sample image: " + DEFAULT_INPUT_IMAGE_URL);
                runOnUiThread(() -> {
                    if (inferenceStatusText != null)
                        inferenceStatusText.setText(
                                "Downloading sample image...\n" + DEFAULT_INPUT_IMAGE_NAME);
                });

                try {
                    URL imgUrl = new URL(DEFAULT_INPUT_IMAGE_URL);
                    HttpURLConnection imgConn = (HttpURLConnection) imgUrl.openConnection();
                    imgConn.setConnectTimeout(15000);
                    imgConn.setReadTimeout(30000);
                    imgConn.connect();

                    if (imgConn.getResponseCode() == HttpURLConnection.HTTP_OK) {
                        try (InputStream imgIn = new BufferedInputStream(imgConn.getInputStream());
                             FileOutputStream imgFos = new FileOutputStream(inputImageFile)) {
                            byte[] buf = new byte[8192];
                            int n;
                            while ((n = imgIn.read(buf)) != -1) {
                                imgFos.write(buf, 0, n);
                            }
                        }
                        imageSuccess = true;
                        Log.d(TAG, "Sample image downloaded: " + inputImageFile.getAbsolutePath());
                    } else {
                        Log.w(TAG, "Sample image download failed, HTTP " + imgConn.getResponseCode());
                    }
                    imgConn.disconnect();
                } catch (Exception imgEx) {
                    // Image download failure is non-fatal; log and continue
                    Log.w(TAG, "Sample image download error (non-fatal): " + imgEx.getMessage());
                }

                // ── Step 2: Unzip the model ───────────────────────────────────
                runOnUiThread(() -> {
                    if (inferenceStatusText != null)
                        inferenceStatusText.setText("Extracting model files...");
                });

                unzip(zipFile, rootDir);
                zipFile.delete();
                Log.d(TAG, "Extraction complete.");
                modelSuccess = true;

            } catch (Exception e) {
                Log.e(TAG, "Auto-download failed: " + e.getMessage(), e);
                errorMsg = e.getMessage();
                if (zipFile.exists()) zipFile.delete();
            }

            final boolean finalModelSuccess = modelSuccess;
            final boolean finalImageSuccess = imageSuccess;
            final String finalError = errorMsg;

            runOnUiThread(() -> {
                // Hide progress indicator
                if (inferenceProgressLayout != null) {
                    inferenceProgressLayout.setVisibility(View.GONE);
                }

                if (finalModelSuccess) {
                    String msg = "Default model downloaded successfully!";
                    if (finalImageSuccess) msg += "\nSample image saved to:\n" + inputImageFile.getAbsolutePath();
                    Toast.makeText(getApplicationContext(), msg, Toast.LENGTH_LONG).show();

                    // Refresh the model list
                    CV_Models.clear();
                    listBinFiles(rootDir, rootDir);
                    if (adapter != null) adapter.notifyDataSetChanged();
                    if (!CV_Models.isEmpty()) {
                        selectedItem = CV_Models.get(0);
                        if (spinner_models != null) spinner_models.setSelection(0);
                    }

                    // Auto-set the downloaded sample image as the input image
                    if (finalImageSuccess && inputImageFile.exists()) {
                        input_img = inputImageFile.getAbsolutePath();
                        Log.d(TAG, "Auto-set input image: " + input_img);
                        Bitmap bmp = BitmapFactory.decodeFile(input_img);
                        if (bmp != null && inputImagePreview != null) {
                            inputImagePreview.setImageBitmap(bmp);
                        }
                    }
                } else {
                    Toast.makeText(getApplicationContext(),
                            "Failed to download default model:\n" + finalError +
                            "\nPlease push model files manually to:\n" + cv_path,
                            Toast.LENGTH_LONG).show();
                    CV_Models.clear();
                    CV_Models.add("(Download failed — push .bin/.dlc to " + cv_path + ")");
                    if (adapter != null) adapter.notifyDataSetChanged();
                }
            });
        }).start();
    }

    /**
     * Unzip a zip file into the given destination directory.
     */
    private void unzip(File zipFile, File destDir) throws IOException {
        try (ZipInputStream zis = new ZipInputStream(
                new BufferedInputStream(new java.io.FileInputStream(zipFile)))) {
            ZipEntry entry;
            byte[] buf = new byte[8192];
            while ((entry = zis.getNextEntry()) != null) {
                File outFile = new File(destDir, entry.getName());
                // Guard against zip-slip
                if (!outFile.getCanonicalPath().startsWith(destDir.getCanonicalPath())) {
                    Log.w(TAG, "Skipping zip entry outside dest dir: " + entry.getName());
                    zis.closeEntry();
                    continue;
                }
                if (entry.isDirectory()) {
                    outFile.mkdirs();
                } else {
                    outFile.getParentFile().mkdirs();
                    try (FileOutputStream fos = new FileOutputStream(outFile)) {
                        int n;
                        while ((n = zis.read(buf)) != -1) {
                            fos.write(buf, 0, n);
                        }
                    }
                    Log.d(TAG, "Extracted: " + outFile.getAbsolutePath());
                }
                zis.closeEntry();
            }
        }
    }

    @Override
    public void onResume(){
        super.onResume();
        if(DEBUG) Log.d(TAG,"onResume,CV_Models=" + CV_Models);

        // Re-scan the model directory every time the Activity resumes.
        // This handles the case where the user just granted storage permission
        // and returned from the system settings page.
        boolean hasPermission = true;
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            hasPermission = android.os.Environment.isExternalStorageManager();
        }

        if (hasPermission) {
            // Permission is now available — re-scan if the list is empty or
            // still contains only the placeholder message (not a real model path).
            boolean needRescan = CV_Models.isEmpty() ||
                    (CV_Models.size() == 1 &&
                     !CV_Models.get(0).endsWith(".bin") &&
                     !CV_Models.get(0).endsWith(".dlc"));
            if (needRescan) {
                Log.d(TAG, "onResume: rescanning model directory");
                showBinFilesList();
            }
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
     * Native method for Java-based pre/post-processing. Java supplies the
     * pre-processed input tensor and receives the raw output tensor; the native
     * library only runs the model inference.
     */
    public native int SuperResolution(String libsDir, String model_path, ByteBuffer inputBuffer, ByteBuffer outputBuffer);

    /**
     * Native method for C++-based pre/post-processing. The native library reads
     * the input image, does all pre/post-processing with OpenCV + xtensor, and
     * writes the output image file.
     */
    public native String SuperResolutionCpp(String libsDir, String model_path, String input_img, String output_img);

    private ByteBuffer preprocess(Bitmap bitmap) {
        // Utils.bitmapToMat converts Android ARGB_8888 Bitmap to a 4-channel RGBA Mat.
        Mat rgba = new Mat();
        Utils.bitmapToMat(bitmap, rgba);

        // Convert RGBA -> RGB to match the model's expected input format.
        // The C++ path uses COLOR_BGR2RGB (imread produces BGR) for the same purpose.
        Mat rgb = new Mat();
        Imgproc.cvtColor(rgba, rgb, Imgproc.COLOR_RGBA2RGB);
        rgba.release();

        // Normalize to [0, 1] float32
        Mat rgbF = new Mat();
        rgb.convertTo(rgbF, CvType.CV_32FC3, 1.0 / 255.0);
        rgb.release();

        int bufferSize = IMAGE_WIDTH * IMAGE_HEIGHT * 3 * 4; // float32
        ByteBuffer byteBuffer = ByteBuffer.allocateDirect(bufferSize);
        byteBuffer.order(ByteOrder.nativeOrder());
        FloatBuffer floatBuffer = byteBuffer.asFloatBuffer();

        float[] data = new float[IMAGE_WIDTH * IMAGE_HEIGHT * 3];
        rgbF.get(0, 0, data);
        rgbF.release();
        floatBuffer.put(data);

        return byteBuffer;
    }

    private Bitmap postprocess(ByteBuffer buffer, int width, int height) {
        buffer.rewind();
        FloatBuffer floatBuffer = buffer.asFloatBuffer();
        float[] data = new float[width * height * 3];
        floatBuffer.get(data);

        // Clamp and scale float [0,1] -> uint8 [0,255]
        for (int i = 0; i < data.length; i++) {
            data[i] = Math.max(0.0f, Math.min(255.0f, data[i] * 255.0f));
        }

        // Build a CV_32FC3 Mat from the output tensor (RGB layout)
        Mat matF = new Mat(height, width, CvType.CV_32FC3);
        matF.put(0, 0, data);

        // Convert float32 RGB -> uint8 RGB
        Mat mat8 = new Mat();
        matF.convertTo(mat8, CvType.CV_8UC3);
        matF.release();

        // Convert RGB (3-channel) -> RGBA (4-channel) so Utils.matToBitmap
        // receives the format it expects (ARGB_8888 Bitmap = RGBA in OpenCV).
        Mat matRgba = new Mat();
        Imgproc.cvtColor(mat8, matRgba, Imgproc.COLOR_RGB2RGBA);
        mat8.release();

        Bitmap bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888);
        Utils.matToBitmap(matRgba, bitmap);
        matRgba.release();

        return bitmap;
    }
}
