//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//=============================================================================

package com.example.geniechat;

import android.Manifest;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.Settings;
import android.text.InputType;
import android.widget.EditText;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AlertDialog;
import androidx.core.content.ContextCompat;
import androidx.preference.ListPreference;
import androidx.preference.Preference;
import androidx.preference.PreferenceCategory;
import androidx.preference.PreferenceFragmentCompat;
import androidx.preference.PreferenceManager;

import java.io.File;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

public class SettingsFragment extends PreferenceFragmentCompat {

    private static final String KEY_MODELS_SET = "custom_models_set";
    private static final String KEY_SELECTED_MODEL = "model_name";
    private static final String DEFAULT_MODEL = "qwen2.0_7b";
    private static final int REQ_READ_EXTERNAL = 1001;

    private SharedPreferences sharedPreferences;
    private PreferenceCategory modelsCategory;

    @Override
    public void onCreatePreferences(Bundle savedInstanceState, String rootKey) {
        setPreferencesFromResource(R.xml.root_preferences, rootKey);

        sharedPreferences = PreferenceManager.getDefaultSharedPreferences(requireContext());
        modelsCategory = findPreference("models_category");


        Preference addModelPref = findPreference("add_model");
        if (addModelPref != null) {
            // we no longer use manual add — hide this control and keep scanning disk
            addModelPref.setVisible(false);
        }

    }

    @Override
    public void onResume() {
        super.onResume();
        ensurePermissionAndLoadModels();
    }

    private void ensurePermissionAndLoadModels() {
        // Android 11+ requires MANAGE_EXTERNAL_STORAGE to access arbitrary external paths
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (Environment.isExternalStorageManager()) {
                List<String> models = scanModelsFromSdcard();
                loadAndDisplayModelsFromDisk(models);
            } else {

                try {
                    Intent intent = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION);
                    intent.setData(Uri.parse("package:" + requireContext().getPackageName()));
                    startActivity(intent);
                } catch (Exception e) {

                    Intent intent = new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION);
                    startActivity(intent);
                }
                Toast.makeText(getContext(), "Please grant file access permissions in the system settings and return to this page to load the model list.", Toast.LENGTH_LONG).show();
            }
        } else {

            if (ContextCompat.checkSelfPermission(requireContext(), Manifest.permission.READ_EXTERNAL_STORAGE)
                    == PackageManager.PERMISSION_GRANTED) {
                List<String> models = scanModelsFromSdcard();
                loadAndDisplayModelsFromDisk(models);
            } else {
                requestPermissions(new String[]{Manifest.permission.READ_EXTERNAL_STORAGE}, REQ_READ_EXTERNAL);
            }
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions, @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_READ_EXTERNAL) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                List<String> models = scanModelsFromSdcard();
                loadAndDisplayModelsFromDisk(models);
            } else {
                Toast.makeText(getContext(), "Storage access is required to load the model list.", Toast.LENGTH_SHORT).show();
                // fallback: show previously saved models if any
                loadAndDisplayModels();
            }
        }
    }


    private void loadAndDisplayModels() {
        if (modelsCategory == null) return;
        Set<String> models = new HashSet<>(sharedPreferences.getStringSet(KEY_MODELS_SET, Collections.emptySet()));
        if (models.isEmpty()) {
            models.add(DEFAULT_MODEL);
            saveModels(models);
        }
        loadAndDisplayModelsFromDisk(new ArrayList<>(models));
    }


    private void loadAndDisplayModelsFromDisk(List<String> models) {
        if (modelsCategory == null) return;


        modelsCategory.removeAll();

        if (models == null || models.isEmpty()) {
            models = new ArrayList<>();
            models.add(DEFAULT_MODEL);
        }


        Collections.sort(models);


        CharSequence[] entries = new CharSequence[models.size()];
        CharSequence[] entryValues = new CharSequence[models.size()];
        for (int i = 0; i < models.size(); i++) {
            entries[i] = models.get(i);
            entryValues[i] = models.get(i);
        }


        ListPreference lp = new ListPreference(requireContext());
        lp.setKey(KEY_SELECTED_MODEL);
        lp.setTitle("Select Model");
        lp.setEntries(entries);
        lp.setEntryValues(entryValues);
        lp.setSummaryProvider(ListPreference.SimpleSummaryProvider.getInstance());


        String currentSelected = sharedPreferences.getString(KEY_SELECTED_MODEL, null);
        if (currentSelected == null && entryValues.length > 0) {
            currentSelected = String.valueOf(entryValues[0]);
            sharedPreferences.edit().putString(KEY_SELECTED_MODEL, currentSelected).apply();
        }
        if (currentSelected != null) {
            lp.setValue(currentSelected);
        }


        lp.setOnPreferenceChangeListener((preference, newValue) -> {
            String sel = String.valueOf(newValue);
            sharedPreferences.edit().putString(KEY_SELECTED_MODEL, sel).apply();

            if (modelsCategory != null) {
                modelsCategory.setSummary("Current Selection: " + sel);
            }
            return true;
        });

        modelsCategory.addPreference(lp);

        if (currentSelected != null) {
            modelsCategory.setSummary("Current Selection: " + currentSelected);
        }
    }


    private List<String> scanModelsFromSdcard() {
        List<String> out = new ArrayList<>();
        File sd = Environment.getExternalStorageDirectory();
        File modelsDir = new File(sd, "GenieModels");
        if (modelsDir.exists() && modelsDir.isDirectory()) {
            File[] children = modelsDir.listFiles();
            if (children != null) {
                for (File f : children) {
                    if (f.isDirectory()) {
                        out.add(f.getName());
                    }
                }
            }
        } else {

            Toast.makeText(getContext(), "/sdcard/GenieModels/ directory is not existed", Toast.LENGTH_SHORT).show();
        }
        return out;
    }

    private void showAddModelDialog(Context context) {
        AlertDialog.Builder builder = new AlertDialog.Builder(context);
        builder.setTitle("Add new Model");

        final EditText input = new EditText(context);
        input.setInputType(InputType.TYPE_CLASS_TEXT);
        input.setHint("Input Model Name");
        builder.setView(input);

        builder.setPositiveButton("Add", (dialog, which) -> {
            String modelName = input.getText().toString().trim();
            if (!modelName.isEmpty()) {
                Set<String> models = new HashSet<>(sharedPreferences.getStringSet(KEY_MODELS_SET, Collections.emptySet()));
                models.add(modelName);
                saveModels(models);
                loadAndDisplayModels(); 
            }
        });
        builder.setNegativeButton("Cancel", (dialog, which) -> dialog.cancel());

        builder.show();
    }

    private void showEditModelDialog(Context context, final String currentName) {
        final CharSequence[] options = {"Select this model", "Rename", "Delete", "Cancel"};

        AlertDialog.Builder builder = new AlertDialog.Builder(context);
        builder.setTitle("Run: " + currentName);
        builder.setItems(options, (dialog, item) -> {
            if (options[item].equals("Select this model")) {
                selectModel(currentName);
            } else if (options[item].equals("Rename")) {
                showRenameModelDialog(context, currentName);
            } else if (options[item].equals("Delete")) {
                showDeleteConfirmDialog(context, currentName);
            } else if (options[item].equals("Cancel")) {
                dialog.dismiss();
            }
        });
        builder.show();
    }

    private void selectModel(String modelName) {
        sharedPreferences.edit().putString(KEY_SELECTED_MODEL, modelName).apply();
        loadAndDisplayModels(); 
    }

    private void showRenameModelDialog(Context context, final String oldName) {
        AlertDialog.Builder builder = new AlertDialog.Builder(context);
        builder.setTitle("Rename Model");

        final EditText input = new EditText(context);
        input.setInputType(InputType.TYPE_CLASS_TEXT);
        input.setText(oldName);
        builder.setView(input);

        builder.setPositiveButton("Save", (dialog, which) -> {
            String newName = input.getText().toString().trim();
            if (!newName.isEmpty() && !newName.equals(oldName)) {
                Set<String> models = new HashSet<>(sharedPreferences.getStringSet(KEY_MODELS_SET, new HashSet<>()));
                models.remove(oldName);
                models.add(newName);
                saveModels(models);


                if (oldName.equals(sharedPreferences.getString(KEY_SELECTED_MODEL, ""))) {
                    selectModel(newName);
                } else {
                    loadAndDisplayModels(); 
                }
            }
        });
        builder.setNegativeButton("Cancel", (dialog, which) -> dialog.cancel());
        builder.show();
    }

    private void showDeleteConfirmDialog(Context context, final String nameToDelete) {
        new AlertDialog.Builder(context)
                .setTitle("Delete Model")
                .setMessage("Sure to delete this model '" + nameToDelete + "' ？")
                .setPositiveButton("Delete", (dialog, which) -> {
                    deleteModel(nameToDelete);
                })
                .setNegativeButton("Cancel", null)
                .show();
    }


    private void deleteModel(String nameToDelete) {
        Set<String> models = new HashSet<>(sharedPreferences.getStringSet(KEY_MODELS_SET, new HashSet<>()));

        if (models.size() <= 1) {
            Toast.makeText(getContext(), "Cannot delete the last model", Toast.LENGTH_SHORT).show();
            return;
        }

        models.remove(nameToDelete);
        saveModels(models);

        if (nameToDelete.equals(sharedPreferences.getString(KEY_SELECTED_MODEL, ""))) {
            String newSelected = new TreeSet<>(models).first();
            selectModel(newSelected);
        } else {
            loadAndDisplayModels(); 
        }
    }

    private void saveModels(Set<String> models) {
        sharedPreferences.edit().putStringSet(KEY_MODELS_SET, models).apply();
    }
}