import os, re, yaml

# Utility functions
def extract_info(info_str):
  info_dict = {}

  action_filter = r'（(.+?)）'
  action = re.search(pattern=action_filter, string=info_str)
  without_action = re.sub(pattern=action_filter, repl='', string=info_str)
  is_behind = len(info_str) == info_str.find('）') + 1
  speech = re.search(pattern=r'：(.+)', string=without_action)
  person = re.search(pattern=r'(.+)：', string=without_action)

  if (action or speech) != None:
    info_dict['人物'] = person.group(1)
    if action != None:
      if speech != None:
        if is_behind:
          info_dict['言语'] = speech.group(1)
          info_dict['动作'] = action.group(1)
        else:
          info_dict['动作'] = action.group(1)
          info_dict['言语'] = speech.group(1)
      else:
        info_dict['动作'] = action.group(1)
    elif speech != None:
      info_dict['言语'] = speech.group(1)

  return info_dict

def get_file_list(suffix='story'):
  file_list = []
  for file_name in os.listdir(os.getcwd()):
    if file_name.endswith('.' + suffix):
      file_list.append(file_name)

  return file_list

def is_number(string):
    try:
        float(string)
        return True
    except ValueError:
        return False

def read_file(file_name):
  with open(file=file_name, mode='r', encoding='utf-8') as file_obj:
    lines = [line[:-1] for line in file_obj.readlines()]

  return lines

def write_yaml(file_name, yaml_dict):
  with open(file=file_name, mode='w', encoding='utf-8') as file_obj:
    yaml.dump(data=yaml_dict, stream=file_obj, default_flow_style=False, allow_unicode=True, sort_keys=False)

def yaml_convert(file_name):
  lines = read_file(file_name)
  file_name_without_suffix = os.path.splitext(file_name)[0]
  if len(lines) > 3 and is_number(file_name_without_suffix):
    yaml_dict = {'序号': int(float(file_name_without_suffix)), '时间': lines[0], '地点': lines[1], '描述': lines[2], '对话': []}
    for line in lines[3:]:
      yaml_dict['对话'].append(extract_info(line))
  
    return yaml_dict
  
  return None

# Main function
def story_to_yaml(file_output='story.yaml'):
  yaml_dict = {'场景': []}
  for file_name in sorted(get_file_list()):
    yaml_dict['场景'].append(yaml_convert(file_name))
  
  write_yaml(file_name=file_output, yaml_dict=yaml_dict)

# Run
story_to_yaml()
