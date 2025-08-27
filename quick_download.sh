#!/bin/bash

# 检查是否提供了文件路径参数
if [ $# -eq 0 ]; then
    # 如果没有参数，使用原来的交互式方式
    read -p "Please input a video link to start: " link
    
    # 定义要执行的命令及参数
    executable='./yt-dlp'
    format="(bv*[vcodec~='^(avc|h264)']+ba[acodec~='^(mp?4a|aac)'])"
    # 参数列表调整为 Bash 格式
    parameters="--cookies-from-browser Firefox --download-archive downloaded.txt -f $format $link"
    # parameters="--cookies cookies.txt --download-archive downloaded.txt -f $format $link"
    
    # 执行命令
    $executable $parameters
    
    # 再次运行，防止遗漏下载
    $executable $parameters
else
    # 如果提供了文件路径参数，使用批量下载
    file_path="$1"
    
    # 定义要执行的命令及参数
    executable='./yt-dlp'
    format="(bv*[vcodec~='^(avc|h264)']+ba[acodec~='^(mp?4a|aac)'])"
    # 参数列表调整为 Bash 格式，使用 -a 参数批量下载
    parameters="--cookies-from-browser Firefox --download-archive downloaded.txt -f $format -a $file_path"
    # parameters="--cookies cookies.txt --download-archive downloaded.txt -f $format -a $file_path"
    
    # 执行命令
    $executable $parameters
    
    # 再次运行，防止遗漏下载
    $executable $parameters
fi
