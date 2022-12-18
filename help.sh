#!/bin/bash

cd ~
cd storage
cd shared
cd obsidian
git pull origin master
git add -A
git commit -m "更新"
git push origin master