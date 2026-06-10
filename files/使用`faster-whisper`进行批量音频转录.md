# 使用`faster-whisper`进行批量音频转录

`ASR`技术是当前对抗信息过剩的有效手段，本教程将给出一种截止目前为止最廉价的批量音频转录方法。

## 测试环境

本教程使用了作为较受欢迎的`Linux`发行版之一的`Linux Mint 22 Cinnamon`，其基于`Ubuntu 24.04 noble`。若你使用了不同的系统，本教程依然具有一定的参考价值。

## 修改软件源（若未修改）

进入`System Settings`->`Software Sources`，根据测速结果，修改软件源为速度较快选项，并应用修改。

## 安装显卡驱动（若未安装）

确保你的上设备中有一张不是太旧的`Nvidia`牌显卡，显存`2GB`及以上即可。进入`System Settings`->`Driver Manager`，安装你认为合适的驱动，并重启设备。

## 安装`Docker Engine`（若未安装）

可参考[官网文档](https://docs.docker.com/engine/install/ubuntu/)，全过程命令如下：

```bash
sudo apt-get update
sudo apt-get install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
```

## 安装`NVIDIA Container Toolkit`（若未安装）

可参考[官网文档](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)，全过程命令如下：

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
export NVIDIA_CONTAINER_TOOLKIT_VERSION=1.17.8-1
sudo apt-get install -y nvidia-container-toolkit=${NVIDIA_CONTAINER_TOOLKIT_VERSION} nvidia-container-toolkit-base=${NVIDIA_CONTAINER_TOOLKIT_VERSION} libnvidia-container-tools=${NVIDIA_CONTAINER_TOOLKIT_VERSION} libnvidia-container1=${NVIDIA_CONTAINER_TOOLKIT_VERSION}

sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

## 下载`faster-whisper`适用模型文件

推荐使用`HuggingFace`平台中的[这个](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo)`whisper-large-v3-turbo`模型进行音频转录。

请下载模型、分词器、词汇表、设置文件，并将它们放入合适的文件夹，在下一步中自行修改模型路径为该文件夹。

## 拉取镜像并创建容器

`Linux`中配置`CUDA`和`CUDNN`库至今不是非常方便，会出现各种奇怪的问题。一种较好的办法是使用现成的官方`Docker`镜像，可以避免几乎所有问题，也便于后期清理，全过程命令如下：

```bash
sudo -i

docker run -d --name cuda123 --gpus all -v /home/$(whoami)/whisper_model:/whisper_model -v /home/$(whoami)/Videos:/files nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04 sleep infinity
```

请自行修改模型文件夹路径及音视频文件夹路径，以及容器名字等。若需回到普通用户状态，轻按`Ctrl`+`D`组合键。

## 安装`whisper-ctranslate2`库

进入容器，然后通过`pip`安装`whisper-ctranslate2`库，全过程命令如下：

```bash
docker exec -it cuda123 /bin/bash

apt-get update && apt-get install -y python3-pip
pip3 install -U whisper-ctranslate2
```

## 编写批量音频转录脚本

在音视频文件夹中，创建一个合适的`Bash`脚本，用于执行批量音频转录，一个示例如下：

```bash
#!/usr/bin/env bash
set -euo pipefail

MODEL_DIR="/whisper_model"
LANG="zh"
INITIAL_PROMPT="注意看 这是一段中文普通话"
DEVICE="cuda"

shopt -s nullglob
for file in *.mp4 *.m4a; do
  base="${file%.*}"
  srt_file="${base}.srt"

  if [[ -e "$srt_file" ]]; then
    echo "跳过：$srt_file 已存在"
    continue
  fi

  echo "处理：$file -> $srt_file"
  whisper-ctranslate2 "$file" \
    --device "$DEVICE" \
    --initial_prompt "$INITIAL_PROMPT" \
    --language "$LANG" \
    --local_files_only True \
    --model_directory "$MODEL_DIR" \
    --output_format srt \
    --vad_filter True \
    --verbose False
done
```

## 执行批量音频转录

切换路径到音视频文件夹，执行下面的命令，即可开始执行批量音频转录。

```bash
bash transcribe.sh
```

祝你使用愉快！

## 常见问题

1. 设备中没有`Nvidia`牌显卡怎么办？
    - 你可跳过几乎所有步骤，使用转录脚本在`CPU`模式下进行转录。在一些现代的`CPU`如`Apple Silicon M4`上，即使没有使用任何加速（`MPS`或`MLX`加速支持目前仍不尽人意），也可以达到三倍速转录。
2. 为什么不使用`Dockerfile`？
    - 因为懒。
3. 转录生成的字幕文件有什么用？
    - 请自行探索用途。
