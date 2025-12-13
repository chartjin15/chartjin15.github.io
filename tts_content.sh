#!/usr/bin/env bash

SPLIT_NUM=1000
RATE=180

if [ $# -eq 0 ]; then
    files=($(find . -maxdepth 1 -type f \( -name "*.txt" -o -name "*.md" \) | sort))
else
    declare -A seen_files
    for file in "$@"; do
        if [ ! -f "$file" ]; then
            continue
        fi
        if [[ "$file" == *.txt ]] || [[ "$file" == *.md ]]; then
            base_name=$(basename "$file")
            key="${base_name%.txt}"
            key="${key%.md}"
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
    name_only="${base_name%.txt}"
    name_only="${name_only%.md}"

    echo "Processing $file..."

    if ls "${name_only}"_*.aiff >/dev/null 2>&1; then
        echo "$file already has TTS output, skipping."
        continue
    fi

    cleaned_content=$(sed -e '/^$/d' -e 's/\r$//' "$file")
    IFS=$'\n' read -d '' -r -a lines <<< "$cleaned_content"
    total_lines=${#lines[@]}

    if [ $total_lines -eq 0 ]; then
        echo "Empty file, skipping: $file"
        continue
    fi

    num_chunks=$(( (total_lines + SPLIT_NUM - 1) / SPLIT_NUM ))

    for ((i = 0; i < num_chunks; i++)); do
        start=$((i * SPLIT_NUM))
        end=$((start + SPLIT_NUM))
        if [ $end -gt $total_lines ]; then
            end=$total_lines
        fi

        chunk_lines=()
        for ((j = start; j < end; j++)); do
            chunk_lines+=("${lines[j]}")
        done
        chunk_content=$(printf "%s\n" "${chunk_lines[@]}")

        padded_index=$(printf "%03d" $((i + 1)))
        output_file="${name_only}_${padded_index}.aiff"

        echo "Generating: $output_file (lines ${start} to ${end})"
        say -r "$RATE" -o "$output_file" <<< "$chunk_content"
    done

    echo "TTS completed for $file -> $num_chunks files generated."
done

echo "All processing complete."
