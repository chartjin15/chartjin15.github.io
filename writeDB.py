import datetime, os, readline, sqlite3, sys

connect = sqlite3.connect(('text_archive' if len(sys.argv) < 2 else sys.argv[1]) + '.db')
cursor = connect.cursor()

cursor.execute('CREATE TABLE IF NOT EXISTS text (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, record_time TEXT NOT NULL)')

timezone = datetime.timezone(datetime.timedelta(hours=8))
cursor.execute('SELECT * FROM text WHERE substr(record_time,1,10) = ? ORDER BY id', (datetime.datetime.now(timezone).strftime('%Y/%m/%d'),))
today_char_count = sum(sum(1 for ch in row[1] if 0x4E00 <= ord(ch) <= 0x9FFF) for row in cursor.fetchall())
while True:
    input_string = input(f'\u4eca\u65e5\u5df2\u8f93\u5165\uff1a{(today_char_count):04}\u5b57\n> ').strip()
    if input_string in ['q', 'quit']:
        connect.close()
        break
    cursor.execute('INSERT INTO text (content, record_time) VALUES (?, ?)', (input_string, datetime.datetime.now(timezone).strftime('%Y/%m/%d %H:%M:%S'),))
    connect.commit()
    today_char_count += sum(1 for ch in input_string if 0x4E00 <= ord(ch) <= 0x9FFF)
    os.system(command='cls' if os.name == 'nt' else 'clear')
