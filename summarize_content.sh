#!/usr/bin/env bash

API_URL="http://localhost:1234/v1/chat/completions"
API_KEY="lm-studio"
MODEL_NAME="qwen3-vl-32b-instruct"
TOP_P=0.8
TOP_K=20
TEMPERATURE=0.7
SYSTEM_PROMPT="You are a helpful assistant response mainly in Simplified Chinese."
INITIAL_PROMPT="Enter something here to start!"
SPLIT_NUM=1500

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
        cleaned_content=$(cat "$file")
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

        chunk_content=$(printf "%s\\\\n" "${chunk_lines[@]}")
        request_content="$INITIAL_PROMPT\n$chunk_content"

        response=$(curl -s "$API_URL" \
          -H "Content-Type: application/json" \
          -H "Authorization: Bearer $API_KEY" \
          -d "{\"model\": \"$MODEL_NAME\", \"messages\": [{\"role\": \"system\", \"content\": \"$SYSTEM_PROMPT\"}, {\"role\": \"user\", \"content\": \"$request_content\"}], \"temperature\": $TEMPERATURE, \"top_p\": $TOP_P, \"top_k\": $TOP_K, \"stream\": false}")

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
