# Before start, run `pip install -U qwen-tts` to install all required modules.

from pathlib import Path
from qwen_tts import Qwen3TTSModel
from soundfile import write
from torch import bfloat16
import os, sys
import time

BATCH_SIZE = 8
REF_AUDIO = '1739124223158167033.wav'
REF_TEXT = '我是隐海修会的教士，菲比。岁主在上，愿你的旅途永远有爱与光明垂耀。'

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

    # model = Qwen3TTSModel.from_pretrained('C:\\Users\\xsjcy\\Qwen3-TTS-12Hz-1.7B-Base', device_map='cuda:0', dtype=bfloat16, attn_implementation='flash_attention_2')
    model = Qwen3TTSModel.from_pretrained('C:\\Users\\xsjcy\\Qwen3-TTS-12Hz-1.7B-Base', device_map='cpu', dtype=bfloat16)

    for index in range(0, len(validated_lines), BATCH_SIZE):
        start_time = time.time()

        try:
            prompt = Qwen3TTSModel.create_voice_clone_prompt(model, ref_audio=REF_AUDIO, ref_text=REF_TEXT, x_vector_only_mode=False)

            batch_lines = validated_lines[index:index + BATCH_SIZE]
            keys = [k for k, _ in batch_lines]
            texts = [t for _, t in batch_lines]
            out_names = [f'{input_basename}_{key}.wav' for key in keys]
            wavs, sample_rate = model.generate_voice_clone(text=texts, language='Chinese', voice_clone_prompt=prompt, max_new_tokens=2048)
            for wav_index, wav in enumerate(wavs):
                write(file=out_names[wav_index], data=wav, samplerate=int(sample_rate))
            cursor_file.write_text(batch_lines[-1][0], encoding='utf-8')
            with open(file=f'{input_basename}.concat', mode='a', encoding='utf-8') as file_obj:
                file_obj.write(''.join([f'file {out_name}\n' for out_name in out_names]))

            del prompt
        except KeyboardInterrupt:
            sys.exit(0)

        end_time = time.time()
        print(f'Time: {end_time - start_time:.2f}, Character Per Second: {sum([len(text) for text in texts]) / (end_time - start_time):.2f}')

    os.system(f'ffmpeg -f concat -i {input_basename}.concat -c:a libmp3lame -b:a 320k {input_basename}.mp3')

if __name__ == '__main__':
    for input_path_str in sys.argv[1:]:
        input_path = Path(input_path_str)
        process_tts(input_path=input_path)
