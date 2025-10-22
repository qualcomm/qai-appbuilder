//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

package com.example.geniechat;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.InputType;
import android.widget.EditText;

import androidx.appcompat.app.AlertDialog;
import androidx.preference.Preference;
import androidx.preference.PreferenceCategory;
import androidx.preference.PreferenceFragmentCompat;
import androidx.preference.PreferenceManager;

import java.util.Collections;
import java.util.HashSet;
import java.util.Set;
import java.util.TreeSet;
import android.widget.Toast;

public class SettingsFragment extends PreferenceFragmentCompat {

    private static final String KEY_MODELS_SET = "custom_models_set";
    private static final String KEY_SELECTED_MODEL = "model_name";
    private static final String DEFAULT_MODEL = "qwen2.0_7b";

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
            addModelPref.setOnPreferenceClickListener(preference -> {
                showAddModelDialog(requireContext());
                return true;
            });
        }

        // 加载并显示模型列表
        loadAndDisplayModels();
    }

    private void loadAndDisplayModels() {
        if (modelsCategory == null) return;

        // 清空旧的模型项 (保留“添加”按钮)
        while (modelsCategory.getPreferenceCount() > 1) {
            modelsCategory.removePreference(modelsCategory.getPreference(0));
        }

        // 读取存储的模型列表，如果为空则添加默认模型
        Set<String> models = new HashSet<>(sharedPreferences.getStringSet(KEY_MODELS_SET, Collections.emptySet()));
        if (models.isEmpty()) {
            models.add(DEFAULT_MODEL);
            saveModels(models);
        }

        // 使用 TreeSet 保证列表有序
        Set<String> sortedModels = new TreeSet<>(models);
        String selectedModel = sharedPreferences.getString(KEY_SELECTED_MODEL, DEFAULT_MODEL);

        int order = 0;
        for (String modelName : sortedModels) {
            Preference modelPref = new Preference(requireContext());
            modelPref.setKey(modelName);
            modelPref.setTitle(modelName);
            // 如果是当前选中的模型，则高亮显示
            if (modelName.equals(selectedModel)) {
                modelPref.setSummary("当前已选择");
            }
            modelPref.setOrder(order++); // 控制显示顺序
            modelPref.setOnPreferenceClickListener(preference -> {
                showEditModelDialog(requireContext(), preference.getTitle().toString());
                return true;
            });
            modelsCategory.addPreference(modelPref);
        }

        // 更新 Category 的摘要，显示当前选择
        modelsCategory.setSummary("当前选择: " + selectedModel);
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