# Before start, run `pip install -U mlx-audio` to install all required modules.

from mlx_audio.tts.utils import load_model
from mlx_audio.utils import load_audio
from numpy import array
from pathlib import Path
from soundfile import write
import os, sys
import time

BATCH_SIZE = 4
REF_AUDIO = load_audio(audio='reference audio path here')
REF_TEXT = 'reference text here'

def split_lines(lines: list[str], seps='。？！：；') -> list[str]:
    for sep in seps:
        lines = [f'{sub_line}{sep}'if sub_line[-1] not in seps else sub_line for line in lines for sub_line in line.split(sep=sep) if sub_line]
    return lines

def process_tts(input_path: Path) -> None:
    with input_path.open(mode='r', encoding='utf-8') as file_obj:
        total_lines = [line.strip().translate(str.maketrans('…', '。', '“”‘’「」『』（）')) for line in file_obj.readlines()]

    validated_lines = []
    for i, line in enumerate(total_lines):
        if line:
            sub_lines = split_lines(lines=[line])
            for sub_i, sub_line in enumerate(sub_lines):
                validated_lines.append((f'{i + 1:05d}_{sub_i + 1:02d}' if len(sub_lines) > 1 else f'{i + 1:05d}', sub_line))
    validated_line_num = len(validated_lines)

    input_basename = input_path.stem

    cursor_file = Path(f'{input_basename}.cursor')
    if cursor_file.exists():
        start_index = next(i for i, item in enumerate(validated_lines) if item[0] == cursor_file.read_text(encoding='utf-8').strip()) + 1
        if start_index < validated_line_num:
            validated_lines = validated_lines[start_index:]
        else:
            return None

    model = load_model('/Users/username/Qwen3-TTS-12Hz-1.7B-Base-8bit') # Change it to the model you prefer

    for index in range(0, len(validated_lines), BATCH_SIZE):
        start_time = time.time()

        try:
            batch_lines = validated_lines[index:index + BATCH_SIZE]
            keys = [k for k, _ in batch_lines]
            texts = [t for _, t in batch_lines]
            out_names = [f'{input_basename}_{key}.wav' for key in keys]
            for result in model.batch_generate(texts=texts, ref_audios=[REF_AUDIO] * len(out_names), ref_texts=[REF_TEXT] * len(out_names), lang_code='zh', max_tokens=2048):
                write(file=out_names[result.sequence_idx], data=array(result.audio), samplerate=24000, subtype='PCM_16')
            cursor_file.write_text(batch_lines[-1][0], encoding='utf-8')
            with open(file=f'{input_basename}.concat', mode='a', encoding='utf-8') as file_obj:
                file_obj.write(''.join([f'file {out_name}\n' for out_name in out_names]))
        except KeyboardInterrupt:
            sys.exit(0)

        end_time = time.time()
        print(f'Time: {end_time - start_time:.2f}, Character Per Second: {sum([len(text) for text in texts]) / (end_time - start_time):.2f}')

    os.system(f'./ffmpeg -f concat -i {input_basename}.concat -c:a libmp3lame -b:a 320k {input_basename}.mp3')

if __name__ == '__main__':
    for input_path_str in sys.argv[1:]:
        input_path = Path(input_path_str)
        process_tts(input_path=input_path)
