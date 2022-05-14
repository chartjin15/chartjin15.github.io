#!/bin/bash

inputFormat="mp4" # 注意要区分大小写
ouputFormat="mkv"
codeV="libx265"
preset="medium" # preset值可取：x264、x265为ultrafast superfast veryfast faster fast medium slow slower veryslow placebo（某字幕组取slower）
crf=28 # crf值可取：x264、x265为-1到51（-1为默认值，分别对应23和28）

if [[ ! -d "output" ]]; then mkdir output; fi

for i in `ls *.$inputFormat`; do ffmpeg -y -hwaccel auto -i "$i" -c:v "$codeV" -preset "$preset" -crf $crf -c:a copy "./output/${i%.*}.$ouputFormat"; done
