#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

# Transcribe any .m4a and .mp4 files to .srt in the current directory
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

# Collect .srt files
files=( *.srt )
if [ ${#files[@]} -eq 0 ]; then
  exit 0
fi

# Sort filenames by fields separated with '_' (primary: field1, secondary: field2),
# then process each file in that order.
printf '%s\n' "${files[@]}" | sort -t_ -k1,1 -k2,2 | while IFS= read -r f; do
  awk '
    BEGIN {
      FS = "-->"
      OFS = ""
      rec = ""
    }

    # Skip index lines (lines that contain only digits)
    /^[0-9]+$/ { next }

    # Detect timestamp lines (contain "-->") and mark that subsequent text lines should be captured
    /-->/ { rec = 1; next }

    # For other lines: if we are in a recorded block and the line is not empty, print it
    {
      if (rec && NF) {
        gsub(/\r$/, "")    # remove any trailing CR (for Windows line endings)
        print $0
      }
    }
  ' "$f"
done > combined.txt
