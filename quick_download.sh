#!/bin/bash

executable='./yt-dlp'
format="(bv*[vcodec~='^(avc|h264)']+ba[acodec~='^(mp?4a|aac)'])"
template="%(id)s.%(ext)s"

if [ $# -eq 0 ]; then
    read -p "Please input a video link to start: " link

    parameters="--cookies-from-browser Firefox --download-archive downloaded.txt -f $format -o $template $link"
    # parameters="--cookies cookies.txt --download-archive downloaded.txt -f $format -o $template $link"

    $executable $parameters
else
    file_path="$1"

    parameters="--cookies-from-browser Firefox --download-archive downloaded.txt -f $format -o $template -a $file_path"
    # parameters="--cookies cookies.txt --download-archive downloaded.txt -f $format -o $template -a $file_path"

    $executable $parameters
fi
