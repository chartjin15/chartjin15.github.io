#!/usr/bin/env bash

API_URL="http://192.168.31.241:2333/v1/chat/completions"
API_KEY="lm-studio"
MODEL_NAME="qwen3-vl-32b-instruct"
TOP_P=0.8
TOP_K=20
TEMPERATURE=0.7
SYSTEM_PROMPT="You are a helpful assistant response mainly in Simplified Chinese."
INITIAL_PROMPT='你将收到一段文本内容。你的任务是：\n**严格按照以下指定的YAML格式模板，用简体中文对所提供文本进行精确归纳。**\n\n请务必做到：\n- **准确无误地提取所有出场角色及其相关信息**；\n- **按时间顺序完整梳理主要剧情发展节点**；\n- **不遗漏任何关键人物、情节或细节**；\n- **输出内容必须完全符合YAML格式，且结构层级严格一致**；\n- **仅使用简体中文进行回答（包括键名和值）**；\n\n严禁：\n- 添加未在原文中出现的角色或事件；\n- 改写剧情顺序或主观推测角色动机；\n- 使用非YAML格式输出（如Markdown、纯文本等）；\n- 在YAML块中输出空行；\n- 输出任何引导性文字（例如“以下是归纳结果”、“根据你的要求”等前缀语句）；\n\n---\n\n### YAML 回答模板（必须严格遵循此结构输出）：\n\n出场角色:\n  - 名称: 角色A\n    简介: （根据被提供文本得到的关于角色A的介绍，包括但不限于姓名、身份、外貌特征、性格特点、关键行为或台词等）\n  - 名称: 角色B\n    简介: （同上）\n  # 按字母顺序或出场顺序列出所有角色\n主要剧情:\n  - 1: （根据被提供文本，按照事件发生的时间顺序，逐条描述第一个重要情节。需包含人物、动作、结果等要素）\n  - 2: （继续按时间顺序描述下一个关键情节，保持逻辑连贯性）\n  - # 依此类推，直到涵盖所有主要剧情节点\n\n---\n\n输出要求：\n- 所有字段必须使用简体中文；\n- YAML 层级结构必须严格遵循上述模板（如出场角色下为数组对象，每个对象含名称和简介字段）；\n- 主要剧情中的序号必须从1开始连续递增，内容不可省略或合并；\n- 不允许使用缩进以外的格式修饰（如加粗、斜体等）；\n\n---\n\n请特别注意：你的输出**必须是纯YAML文本**，无需任何解释性文字。'
SPLIT_NUM=500

if [ $# -eq 0 ]; then
    files=($(find . -maxdepth 1 -type f \( -name "*.srt" -o -name "*.txt" \) | sort))
else
    declare -A seen_files
    for file in "$@"; do
        if [ ! -f "$file" ]; then
            continue
        fi
        if [[ "$file" == *.srt ]] || [[ "$file" == *.txt ]]; then
            base_name=$(basename "$file")
            key="${base_name%.srt}"
            key="${key%.txt}.md"
            if [ -z "${seen_files[$key]}" ]; then
                files+=("$file")
                seen_files[$key]=1
            fi
        fi
    done
    if [ ${#files[@]} -eq 0 ]; then
        exit 0
    fi
fi

for file in "${files[@]}"; do
    base_name=$(basename "$file")
    md_file="${base_name%.srt}"
    md_file="${md_file%.txt}.md"

    if [ -f "$md_file" ]; then
        continue
    fi

    echo "Processing $file..."

    if [[ "$file" == *.srt ]]; then
        cleaned_content=$(grep -vE '^[0-9]+$|^[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3}\s-->\s[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3}$|^$' "$file")
    else
        cleaned_content=$(sed 's/\r$//' "$file")
    fi

    IFS=$'\n' read -d '' -r -a lines <<< "$cleaned_content"

    first_chunk=true
    for ((start = 0; start < ${#lines[@]}; start += SPLIT_NUM)); do
        end=$((start + SPLIT_NUM))
        if [ $end -gt ${#lines[@]} ]; then
            end=${#lines[@]}
        fi

        chunk_lines=()
        for ((i = start; i < end; i++)); do
            chunk_lines+=("${lines[i]}")
        done

        chunk_content=$(printf "%s\n" "${chunk_lines[@]}")
        escaped_prompt=$(echo -e "$INITIAL_PROMPT\n$chunk_content" | jq -Rr @json)

        response=$(curl -s "$API_URL" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer $API_KEY" \
            -d "$(jq -n --arg model "$MODEL_NAME" --arg system_prompt "$SYSTEM_PROMPT" --arg user_content "$escaped_prompt" '{model: $model, messages: [{role: "system", content: $system_prompt}, {role: "user", content: $user_content}], temperature: '$TEMPERATURE', top_p: '$TOP_P', top_k: '$TOP_K', stream: false}')")

        summary=$(echo "$response" | jq -r '.choices[0].message.content')

        if [ "$first_chunk" = true ]; then
            echo "$summary" > "$md_file"
            first_chunk=false
        else
            echo "" >> "$md_file"
            printf "%*s\n" 50 | tr ' ' '-' >> "$md_file"
            echo "" >> "$md_file"
            echo "$summary" >> "$md_file"
        fi
    done

    echo "Summary saved to $md_file"
done

echo "Processing complete."
