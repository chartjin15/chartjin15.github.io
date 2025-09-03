#!/bin/bash

# Termux获取外部存储访问权限
termux-setup-storage
# Git用户名及邮箱设置
git config --global user.name "Your Name"
git config --global user.email "youremail@yourdomain.com"
# 部分设备开启保存用户名及密码
git config --global credential.helper store
# 将克隆文件夹作为安全文件夹
git config --global --add safe.directory /storage/emulated/0/obsidian
# 导航到用户根目录
cd ~
cd storage
cd shared
# 克隆笔记仓库
git clone https://gitee.com/chartjin15/obsidian.git
