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

        // 设置“添加新模型”按钮的点击事件
        Preference addModelPref = findPreference("add_model");
        if (addModelPref != null) {
            // we no longer use manual add — hide this control and keep scanning disk
            addModelPref.setVisible(false);
        }

        // 不在此处直接加载模型，改为在 onResume 时检查权限并动态加载
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
                // 引导用户到系统设置页面授予 All files access
                try {
                    Intent intent = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION);
                    intent.setData(Uri.parse("package:" + requireContext().getPackageName()));
                    startActivity(intent);
                } catch (Exception e) {
                    // 备用通用页面
                    Intent intent = new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION);
                    startActivity(intent);
                }
                Toast.makeText(getContext(), "请在系统设置中授予文件访问权限，然后返回本页面以加载模型列表", Toast.LENGTH_LONG).show();
            }
        } else {
            // Android 10 及以下使用 READ_EXTERNAL_STORAGE
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
                Toast.makeText(getContext(), "需要读取存储权限以加载模型列表", Toast.LENGTH_SHORT).show();
                // fallback: show previously saved models if any
                loadAndDisplayModels();
            }
        }
    }

    // 原来的基于 SharedPreferences 的列表保留作为后备
    private void loadAndDisplayModels() {
        if (modelsCategory == null) return;
        Set<String> models = new HashSet<>(sharedPreferences.getStringSet(KEY_MODELS_SET, Collections.emptySet()));
        if (models.isEmpty()) {
            models.add(DEFAULT_MODEL);
            saveModels(models);
        }
        loadAndDisplayModelsFromDisk(new ArrayList<>(models));
    }

    /**
     * 根据磁盘上扫描到的模型名刷新设置界面（使用 ListPreference）
     */
    private void loadAndDisplayModelsFromDisk(List<String> models) {
        if (modelsCategory == null) return;

        // 清空 modelsCategory 中的旧项
        modelsCategory.removeAll();

        // 如果没有找到任何模型，使用默认模型占位
        if (models == null || models.isEmpty()) {
            models = new ArrayList<>();
            models.add(DEFAULT_MODEL);
        }

        // 保证有序
        Collections.sort(models);

        // 转换为 entries/entryValues
        CharSequence[] entries = new CharSequence[models.size()];
        CharSequence[] entryValues = new CharSequence[models.size()];
        for (int i = 0; i < models.size(); i++) {
            entries[i] = models.get(i);
            entryValues[i] = models.get(i);
        }

        // 创建 ListPreference 供用户选择
        ListPreference lp = new ListPreference(requireContext());
        lp.setKey(KEY_SELECTED_MODEL);
        lp.setTitle("选择模型");
        lp.setEntries(entries);
        lp.setEntryValues(entryValues);
        lp.setSummaryProvider(ListPreference.SimpleSummaryProvider.getInstance());

        // 确保 ListPreference 显示当前已选值
        String currentSelected = sharedPreferences.getString(KEY_SELECTED_MODEL, null);
        if (currentSelected == null && entryValues.length > 0) {
            currentSelected = String.valueOf(entryValues[0]);
            sharedPreferences.edit().putString(KEY_SELECTED_MODEL, currentSelected).apply();
        }
        if (currentSelected != null) {
            lp.setValue(currentSelected);
        }

        // 当用户更改选择时，立即保存并更新 Category 的摘要以即时反映
        lp.setOnPreferenceChangeListener((preference, newValue) -> {
            String sel = String.valueOf(newValue);
            sharedPreferences.edit().putString(KEY_SELECTED_MODEL, sel).apply();
            // 更新 category 摘要，通知 UI 立即刷新
            if (modelsCategory != null) {
                modelsCategory.setSummary("当前选择: " + sel);
            }
            return true;
        });

        modelsCategory.addPreference(lp);
        // 显示当前选择在 Category 摘要（初始）
        if (currentSelected != null) {
            modelsCategory.setSummary("当前选择: " + currentSelected);
        }
    }

    /**
     * 扫描 /sdcard/GenieModels/ 下的子目录并返回目录名列表
     */
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
            // 目录不存在，提示并返回空，让后续显示默认
            Toast.makeText(getContext(), "/sdcard/GenieModels/ 目录不存在", Toast.LENGTH_SHORT).show();
        }
        return out;
    }

    private void showAddModelDialog(Context context) {
        AlertDialog.Builder builder = new AlertDialog.Builder(context);
        builder.setTitle("添加新模型");

        final EditText input = new EditText(context);
        input.setInputType(InputType.TYPE_CLASS_TEXT);
        input.setHint("输入模型名称");
        builder.setView(input);

        builder.setPositiveButton("添加", (dialog, which) -> {
            String modelName = input.getText().toString().trim();
            if (!modelName.isEmpty()) {
                Set<String> models = new HashSet<>(sharedPreferences.getStringSet(KEY_MODELS_SET, Collections.emptySet()));
                models.add(modelName);
                saveModels(models);
                loadAndDisplayModels(); // 刷新列表
            }
        });
        builder.setNegativeButton("取消", (dialog, which) -> dialog.cancel());

        builder.show();
    }

    private void showEditModelDialog(Context context, final String currentName) {
        final CharSequence[] options = {"选择此模型", "重命名", "删除", "取消"};

        AlertDialog.Builder builder = new AlertDialog.Builder(context);
        builder.setTitle("操作: " + currentName);
        builder.setItems(options, (dialog, item) -> {
            if (options[item].equals("选择此模型")) {
                selectModel(currentName);
            } else if (options[item].equals("重命名")) {
                showRenameModelDialog(context, currentName);
            } else if (options[item].equals("删除")) {
                showDeleteConfirmDialog(context, currentName);
            } else if (options[item].equals("取消")) {
                dialog.dismiss();
            }
        });
        builder.show();
    }

    private void selectModel(String modelName) {
        sharedPreferences.edit().putString(KEY_SELECTED_MODEL, modelName).apply();
        loadAndDisplayModels(); // 刷新列表以更新“当前已选择”的摘要
    }

    private void showRenameModelDialog(Context context, final String oldName) {
        AlertDialog.Builder builder = new AlertDialog.Builder(context);
        builder.setTitle("重命名模型");

        final EditText input = new EditText(context);
        input.setInputType(InputType.TYPE_CLASS_TEXT);
        input.setText(oldName);
        builder.setView(input);

        builder.setPositiveButton("保存", (dialog, which) -> {
            String newName = input.getText().toString().trim();
            if (!newName.isEmpty() && !newName.equals(oldName)) {
                Set<String> models = new HashSet<>(sharedPreferences.getStringSet(KEY_MODELS_SET, new HashSet<>()));
                models.remove(oldName);
                models.add(newName);
                saveModels(models);

                // 如果被重命名的是当前选中的模型，则更新选中的模型名称
                if (oldName.equals(sharedPreferences.getString(KEY_SELECTED_MODEL, ""))) {
                    selectModel(newName);
                } else {
                    loadAndDisplayModels(); // 否则只刷新列表
                }
            }
        });
        builder.setNegativeButton("取消", (dialog, which) -> dialog.cancel());
        builder.show();
    }

    private void showDeleteConfirmDialog(Context context, final String nameToDelete) {
        new AlertDialog.Builder(context)
                .setTitle("删除模型")
                .setMessage("确定要删除模型 '" + nameToDelete + "' 吗？")
                .setPositiveButton("删除", (dialog, which) -> {
                    deleteModel(nameToDelete);
                })
                .setNegativeButton("取消", null)
                .show();
    }


    private void deleteModel(String nameToDelete) {
        Set<String> models = new HashSet<>(sharedPreferences.getStringSet(KEY_MODELS_SET, new HashSet<>()));
        // 不允许删除最后一个模型
        if (models.size() <= 1) {
            Toast.makeText(getContext(), "无法删除最后一个模型", Toast.LENGTH_SHORT).show();
            return;
        }

        models.remove(nameToDelete);
        saveModels(models);

        // 如果被删除的是当前选中的模型，则自动选择列表中的第一个作为新的选中模型
        if (nameToDelete.equals(sharedPreferences.getString(KEY_SELECTED_MODEL, ""))) {
            String newSelected = new TreeSet<>(models).first();
            selectModel(newSelected);
        } else {
            loadAndDisplayModels(); // 否则只刷新列表
        }
    }

    private void saveModels(Set<String> models) {
        sharedPreferences.edit().putStringSet(KEY_MODELS_SET, models).apply();
    }
}