#!/bin/bash

# Git用户名及邮箱设置
# git config --global user.name "Your Name"
# git config --global user.email "youremail@yourdomain.com"
# 部分设备开启保存用户名及密码
# git config --global credential.helper store
# Termux获取外部存储访问权限
# termux-setup-storage

cd ~
cd storage
cd shared
cd obsidian
git pull origin master
git add -A
git commit -m "更新"
git push origin master